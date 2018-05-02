##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
#
# value operations 
# handles regauests to read/write chunk data
# 
#
import asyncio
import json
import time
import numpy as np
from aiohttp.errors import HttpBadRequest, HttpProcessingError 
from aiohttp.web import StreamResponse
from util.arrayUtil import bytesArrayToList, bytesToArray, arrayToBytes
from util.httpUtil import  jsonResponse
from util.idUtil import getS3Key, validateInPartition, isValidUuid
from util.s3Util import  isS3Obj, getS3Bytes, deleteS3Obj   
from util.hdf5dtype import createDataType
from util.dsetUtil import  getSelectionShape, getSliceQueryParam
from util.dsetUtil import getFillValue, getChunkLayout, getEvalStr, getDeflateLevel
from util.chunkUtil import getChunkIndex, getChunkCoordinate, getChunkRelativePoint, getDatasetId


import hsds_logger as log

"""
Update the requested chunk/selection
"""
async def PUT_Chunk(request):
    log.request(request)
    app = request.app 
    #loop = app["loop"]

    task_count = len(asyncio.Task.all_tasks())
    log.debug("Task count: {}".format(task_count))
     
    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)
  
    if not request.has_body:
        msg = "PUT Value with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    content_type = "application/octet-stream"
    if "Content-Type" in request.headers:
        # client should use "application/octet-stream" for binary transfer
        content_type = request.headers["Content-Type"]
    if content_type != "application/octet-stream":
        msg = "Unexpected content_type: {}".format(content_type)
        log.error(msg)
        raise HttpBadRequest(message=msg)

    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(request.GET.keys())))
    if "dset" not in request.GET:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    dset_json = json.loads(request.GET["dset"])
    log.debug("dset_json: {}".format(dset_json))
    dims = getChunkLayout(dset_json)
    deflate_level = getDeflateLevel(dset_json)
    log.info("got deflate_level: {}".format(deflate_level))
    if "root" not in dset_json:
        msg = "expected root key in dset_json"
        log.error(msg)
        raise KeyError(msg)
    rootid = dset_json["root"]
    
    rank = len(dims)  
   
    fill_value = getFillValue(dset_json)
     
    # get chunk selection from query params
    selection = []
    for i in range(rank):
        dim_slice = getSliceQueryParam(request, i, dims[i])
        selection.append(dim_slice)   
    selection = tuple(selection)  
    log.debug("got selection: {}".format(selection))

    type_json = dset_json["type"]
    itemsize = 'H5T_VARIABLE'  
    if "size" in type_json:
        itemsize = type_json["size"]
    dt = createDataType(type_json)
    log.debug("dtype: {}".format(dt))
    
    if rank == 0:
        msg = "No dimension passed to PUT chunk request"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    if len(selection) != rank:
        msg = "Selection rank does not match shape rank"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    for i in range(rank):
        s = selection[i]
        log.debug("selection[{}]: {}".format(i, s))

    input_shape = getSelectionShape(selection)
    log.debug("input_shape: {}".format(input_shape))
    num_elements = 1
    for extent in input_shape:
        num_elements *= extent
        
    # check that the content_length is what we expect
    if itemsize != 'H5T_VARIABLE':
        log.debug("expect content_length: {}".format(num_elements*itemsize))
    log.debug("actual content_length: {}".format(request.content_length))

    if itemsize != 'H5T_VARIABLE' and (num_elements * itemsize) != request.content_length:
        msg = "Expected content_length of: {}, but got: {}".format(num_elements*itemsize, request.content_length)
        log.error(msg)
        raise HttpBadRequest(message=msg)

    # if the chunk cache has too many dirty items, wait till items get flushed to S3
    chunk_cache = app['chunk_cache']
    log.debug("PUT_Chunk cache utilization: {} dirty_count: {}".format(chunk_cache.cacheUtilizationPercent, chunk_cache.dirtyCount))
    while chunk_cache.cacheUtilizationPercent > 100 and chunk_cache.dirtyCount > 0:
        log.info("PUT_Chunk, cache utilization: {}, sleeping till items are flushed".format(chunk_cache.cacheUtilizationPercent))
        await asyncio.sleep(0)

    # create a numpy array for incoming data
    input_bytes = await request.read()  # TBD - will it cause problems when failures are raised before reading data?
    if len(input_bytes) != request.content_length:
        msg = "Read {} bytes, expecting: {}".format(len(input_bytes), request.content_length)
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")
        
    #input_arr = np.fromstring(input_bytes, dtype=dt)
    input_arr = bytesToArray(input_bytes, dt, input_shape)
    #log.info("input arr: {}".format(input_arr))
    #input_arr = input_arr.reshape(input_shape)

    chunk_arr = None 
    if deflate_level is not None:
        deflate_map = app['deflate_map']
        dset_id = getDatasetId(chunk_id)
        if dset_id not in deflate_map:
            # save the deflate level so the lazy chunk writer can access it
            deflate_map[dset_id] = deflate_level
            log.info("update deflate_map: {}: {}".format(dset_id, deflate_level))
    
    s3_key = getS3Key(chunk_id)
    log.debug("PUT_Chunks s3_key: {}".format(s3_key))
    if chunk_id in chunk_cache:
        chunk_arr = chunk_cache[chunk_id]
    else:
        obj_exists = await isS3Obj(app, s3_key)
        # TBD - potential race condition?
        if obj_exists:
            log.debug("Reading chunk from S3")
            chunk_bytes = await getS3Bytes(app, s3_key, deflate_level=deflate_level)
            chunk_arr = np.fromstring(chunk_bytes, dtype=dt)
            chunk_arr = chunk_arr.reshape(dims)
        else:
            log.debug("Initializing chunk {}".format(chunk_id))
            if fill_value:
                # need to convert list to tuples for numpy broadcast
                if isinstance(fill_value, list):
                    fill_value = tuple(fill_value)
                chunk_arr = np.empty(dims, dtype=dt, order='C')
                chunk_arr[...] = fill_value
            else:
                chunk_arr = np.zeros(dims, dtype=dt, order='C')
        chunk_cache[chunk_id] = chunk_arr
        

    log.info("PUT_Chunk dirty cache count: {}".format(chunk_cache.dirtyCount))
    # update chunk array
    chunk_arr[selection] = input_arr
    chunk_cache.setDirty(chunk_id)

    # async write to S3   
    dirty_ids = app["dirty_ids"]
    now = int(time.time())
    dirty_ids[chunk_id] = now

    # set notify obj for AN
    notify_map = app['notify_map']
    if chunk_id in notify_map:
        notify_obj = notify_map[chunk_id]
        if "root" not in notify_obj or notify_obj["rootid"] != rootid:
            log.error('invalid root in notify_obj: {}'.format(notify_obj))
    else:
        notify_obj[chunk_id] = {"id": chunk_id, "rootid": rootid}
    notify_map[chunk_id] = notify_obj
    
    # chunk update successful     
    resp = await jsonResponse(request, {}, status=201)
    log.response(request, resp=resp)
    return resp


"""
Return data from requested chunk and selection
"""
async def GET_Chunk(request):
    log.request(request)
    app = request.app 
    #loop = app["loop"]

    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(request.GET.keys())))
    if "dset" not in request.GET:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    dset_json = json.loads(request.GET["dset"])
    
    log.debug("dset_json: {}".format(dset_json)) 
    type_json = dset_json["type"]
     
    dims = getChunkLayout(dset_json)
    log.debug("got dims: {}".format(dims))
    rank = len(dims)  

    # get deflate compression level
    deflate_level = getDeflateLevel(dset_json)
    if deflate_level is None:
        log.info("deflate_level is None")
    else:
        log.info("deflate_level: {}".format(deflate_level))
         
    # get chunk selection from query params
    if "select" in request.GET:
        log.debug("select: {}".format(request.GET["select"]))
    selection = []
    for i in range(rank):
        dim_slice = getSliceQueryParam(request, i, dims[i])
        selection.append(dim_slice)   
    selection = tuple(selection)  
    log.debug("got selection: {}".format(selection))

    dt = createDataType(type_json)
    log.debug("dtype: {}".format(dt))

    rank = len(dims)
    if rank == 0:
        msg = "No dimension passed to GET chunk request"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    if len(selection) != rank:
        msg = "Selection rank does not match shape rank"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    for i in range(rank):
        s = selection[i]
        log.debug("selection[{}]: {}".format(i, s))

    # get numpy array of chunk
    chunk_arr = None 
    chunk_cache = app['chunk_cache'] 
    
    if chunk_id in chunk_cache:
        chunk_arr = chunk_cache[chunk_id]
    else:
        s3_key = getS3Key(chunk_id)
        log.debug("GET_Chunks s3_key: {}".format(s3_key))
        # check to see if there's a chunk object
        # TBD - potential race condition?
        obj_exists = await isS3Obj(app, s3_key)
        if not obj_exists:
            # return a 404
            msg = "Chunk {} does not exist".format(chunk_id)
            log.info(msg)
            raise HttpProcessingError(code=404, message="Not found")
        log.debug("Reading chunk {} from S3".format(s3_key))
        chunk_bytes = await getS3Bytes(app, s3_key, deflate_level=deflate_level)
        #chunk_arr = np.fromstring(chunk_bytes, dtype=dt)
        chunk_arr = bytesToArray(chunk_bytes, dt, dims)
        log.debug("chunk size: {}".format(chunk_arr.size))
        #chunk_arr = chunk_arr.reshape(dims)
        chunk_cache[chunk_id] = chunk_arr  # store in cache
     
    resp = None
    
    if "query" in request.GET:
        # do query selection
        query = request.GET["query"]
        log.info("query: {}".format(query))
        if rank != 1:
            msg = "Query selection only supported for one dimensional arrays"
            log.warn(msg)
            raise HttpBadRequest(message=msg)

        limit = 0
        if "Limit" in request.GET:
            limit = int(request.GET["Limit"])

        values = []
        indices = []
        field_names = [] 
        if dt.fields:
            field_names = list(dt.fields.keys())

        x = chunk_arr[selection]
        log.debug("x: {}".format(x))
        eval_str = getEvalStr(query, "x", field_names)
        log.debug("eval_str: {}".format(eval_str))
        where_result = np.where(eval(eval_str))
        log.debug("where_result: {}".format(where_result))
        where_result_index = where_result[0]
        log.debug("whare_result index: {}".format(where_result_index))
        log.debug("boolean selection: {}".format(x[where_result_index]))
        s = selection[0]
        count = 0
        for index in where_result_index:
            log.debug("index: {}".format(index))
            value = x[index].tolist()
            log.debug("value: {}".format(value))
            json_val = bytesArrayToList(value)
            log.debug("json_value: {}".format(json_val))
            json_index = index.tolist() * s.step + s.start  # adjust for selection
            indices.append(json_index)
            values.append(json_val)
            count += 1
            if limit > 0 and count >= limit:
                log.info("got limit items")
                break
         
        query_result = {}
        query_result["index"] = indices
        query_result["value"] = values
        log.info("query_result: {}".format(query_result))
        resp = await jsonResponse(request, query_result)
    else:
        # get requested data
        resp = StreamResponse(status=200)
        resp.headers['Content-Type'] = "application/octet-stream" #binary response
        output_arr = chunk_arr[selection]
        output_data = arrayToBytes(output_arr)
        resp = StreamResponse(status=200)
     
        # write response    
        resp.content_length = len(output_data)
        await resp.prepare(request)
        resp.write(output_data)
        await resp.write_eof()
    return resp

"""
Return data from requested chunk and point selection
"""
async def POST_Chunk(request):
    log.request(request)
    app = request.app 
    #loop = app["loop"]

    put_points = False
    num_points = 0
    if "count" in request.GET:
        num_points = int(request.GET["count"])

    if "action" in request.GET and request.GET["action"] == "put":
        log.info("POST Chunk put points, num_points: {}".format(num_points))

        put_points = True
    else:
        log.info("POST Chunk get points")

    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    log.info("POST chunk_id: {}".format(chunk_id))
    chunk_index = getChunkIndex(chunk_id)
    log.debug("chunk_index: {}".format(chunk_index))
    
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(request.GET.keys())))
    if "dset" not in request.GET:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    dset_json = json.loads(request.GET["dset"])
    log.debug("dset_json: {}".format(dset_json))
    chunk_layout = getChunkLayout(dset_json)
    chunk_coord = getChunkCoordinate(chunk_id, chunk_layout)
    log.debug("chunk_coord: {}".format(chunk_coord))
    deflate_level = getDeflateLevel(dset_json)
    
    if not request.has_body:
        msg = "POST Value with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    content_type = "application/octet-stream"
    if "Content-Type" in request.headers:
        # client should use "application/octet-stream" for binary transfer
        content_type = request.headers["Content-Type"]
    if content_type != "application/octet-stream":
        msg = "Unexpected content_type: {}".format(content_type)
        log.error(msg)
        raise HttpBadRequest(message=msg)
     
    type_json = dset_json["type"]
    dset_dtype = createDataType(type_json)
    log.debug("dtype: {}".format(dset_dtype))

    dims = getChunkLayout(dset_json)
    log.debug("got dims: {}".format(dims))
    rank = len(dims)
    if rank == 0:
        msg = "POST chunk request with no dimensions"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    fill_value = getFillValue(dset_json)

    chunk_arr = None 
    chunk_cache = app['chunk_cache'] 
    
    if chunk_id in chunk_cache:
        chunk_arr = chunk_cache[chunk_id]
    else:
        s3_key = getS3Key(chunk_id)
        log.debug("GET_Chunks s3_key: {}".format(s3_key))
        # check to see if there's a chunk object
        # TBD - potential race condition?
        obj_exists = await isS3Obj(app, s3_key)
        if not obj_exists:
            if put_points:
                # initialize a new chunk
                log.debug("Initializing chunk {}".format(chunk_id))
                if fill_value:
                    # need to convert list to tuples for numpy broadcast
                    if isinstance(fill_value, list):
                        fill_value = tuple(fill_value)
                    chunk_arr = np.empty(dims, dtype=dset_dtype, order='C')
                    chunk_arr[...] = fill_value
                else:
                    chunk_arr = np.zeros(dims, dtype=dset_dtype, order='C')
            else:
                # return a 404
                msg = "Chunk {} does not exist".format(chunk_id)
                log.warn(msg)
                raise HttpProcessingError(code=404, message="Not found")
        if obj_exists:
            log.debug("Reading chunk {} from S3".format(s3_key))
            chunk_bytes = await getS3Bytes(app, s3_key, deflate_level=deflate_level)
            chunk_arr = np.fromstring(chunk_bytes, dtype=dset_dtype)
            chunk_arr = chunk_arr.reshape(dims)
            chunk_cache[chunk_id] = chunk_arr  # store in cache
        else:
            log.debug("Initializing chunk")
            if fill_value:
                # need to convert list to tuples for numpy broadcast
                if isinstance(fill_value, list):
                    fill_value = tuple(fill_value)
                chunk_arr = np.empty(dims, dtype=dset_dtype, order='C')
                chunk_arr[...] = fill_value
            else:
                chunk_arr = np.zeros(dims, dtype=dset_dtype, order='C')
            chunk_cache[chunk_id] = chunk_arr

    # create a numpy array for incoming points
    input_bytes = await request.read()  # TBD - will it cause problems when failures are raised before reading data?
    if len(input_bytes) != request.content_length:
        msg = "Read {} bytes, expecting: {}".format(len(input_bytes), request.content_length)
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")


    if put_points:
        # writing point data

        # create a numpy array with the following type:
        #       (coord1, coord2, ...) | dset_dtype
        if rank == 1:
            coord_type_str = "uint64"
        else:
            coord_type_str = "({},)uint64".format(rank)
        comp_dtype = np.dtype([("coord", np.dtype(coord_type_str)), ("value", dset_dtype)])
        point_arr = np.fromstring(input_bytes, dtype=comp_dtype)
        if len(point_arr) != num_points:
            msg = "Unexpected size of point array, got: {} expected: {}".format(len(point_arr), num_points)
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        for i in range(num_points):
            elem = point_arr[i]
            if rank == 1:
                coord = int(elem[0])
            else:
                coord = tuple(elem[0]) # index to update
            val = elem[1]   # value 
            chunk_arr[coord] = val # update the point
        chunk_cache.setDirty(chunk_id)

        # async write to S3   
        dirty_ids = app["dirty_ids"]
        now = int(time.time())
        dirty_ids[chunk_id] = now
        log.info("set {} to dirty".format(chunk_id))

        # set notify flag for AN
        notify_ids = app['notify_ids']
        notify_ids.add(chunk_id)
    
    else:
        # reading point data  
        point_dt = np.dtype('uint64')  # use unsigned long for point index  
        point_arr = np.fromstring(input_bytes, dtype=point_dt)  # read points as unsigned longs
        if len(point_arr) % rank != 0:
            msg = "Unexpected size of point array"
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        num_points = len(point_arr) // rank
        log.debug("got {} points".format(num_points))

        point_arr = point_arr.reshape((num_points, rank))    
        output_arr = np.zeros((num_points,), dtype=dset_dtype)
    
        for i in range(num_points):
            point = point_arr[i,:]
            tr_point = getChunkRelativePoint(chunk_coord, point)
            val = chunk_arr[tuple(tr_point)]
            output_arr[i] = val
     
    if put_points:
        # write empty response
        resp = await jsonResponse(request, {})
    else:
        # write response
        resp = StreamResponse(status=200)
        resp.headers['Content-Type'] = "application/octet-stream"
        output_data = output_arr.tobytes()
        resp.content_length = len(output_data)
        await resp.prepare(request)
        resp.write(output_data)
        await resp.write_eof()
    return resp

async def DELETE_Chunk(request):
    """HTTP DELETE method for /chunks/
    Note: clients (i.e. SN nodes) don't directly delete chunks.  This method should
    only be called by the AN node.
    """
    log.request(request)
    app = request.app
    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HttpBadRequest(message=msg)
    log.info("DELETE chunk: {}".format(chunk_id))

    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    validateInPartition(app, chunk_id)

    chunk_cache = app['chunk_cache'] 
    s3_key = getS3Key(chunk_id)
    log.debug("DELETE_Chunk s3_key: {}".format(s3_key))

    if chunk_id in chunk_cache:
        del chunk_cache[chunk_id]
    
    await deleteS3Obj(app, s3_key)
        
    resp_json = {  } 

    deflate_map = app["deflate_map"]
    dset_id = getDatasetId(chunk_id)
    if dset_id in deflate_map:
        # The only reason chunks are ever deleted is if the dataset is being deleted,
        # so it should be save to remove this entry now
        log.info("Removing deflate_map entry for {}".format(dset_id))
        del deflate_map[dset_id]
      
    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp
 
 




