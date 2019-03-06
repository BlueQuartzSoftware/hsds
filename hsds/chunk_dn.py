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
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound, HTTPInternalServerError, HTTPServiceUnavailable
from aiohttp.web import json_response, StreamResponse

from util.httpUtil import  request_read
from util.arrayUtil import bytesArrayToList, bytesToArray, arrayToBytes
from util.idUtil import getS3Key, validateInPartition, isValidUuid
from util.s3Util import  isS3Obj, getS3Bytes   
from util.hdf5dtype import createDataType
from util.dsetUtil import  getSelectionShape, getSliceQueryParam
from util.dsetUtil import getFillValue, getChunkLayout, getEvalStr, getDeflateLevel
from util.chunkUtil import getChunkIndex, getChunkCoordinate, getChunkRelativePoint, getDatasetId


import hsds_logger as log

"""
Get the deflate level for given dset
"""
def getDeflate(app, dset_id, dset_json):
    deflate_level = getDeflateLevel(dset_json)
    log.debug("got deflate_level: {}".format(deflate_level))
    if deflate_level is not None:
        deflate_map = app['deflate_map']  
        if dset_id not in deflate_map:
            # save the deflate level so the lazy chunk writer can access it
            deflate_map[dset_id] = deflate_level
            log.debug("update deflate_map: {}: {}".format(dset_id, deflate_level))
    return deflate_level

"""
Utility method for GET_Chunk, PUT_Chunk, and POST_CHunk
Get a numpy array for the chunk (possibly initizaling a new chunk if requested) 
"""
async def getChunk(app, chunk_id, dset_json, chunk_init=False):
    # if the chunk cache has too many dirty items, wait till items get flushed to S3
    MAX_WAIT_TIME = 10.0  # TBD - make this a config
    chunk_cache = app['chunk_cache']
    log.debug(f"getChunk cache utilization: {chunk_cache.cacheUtilizationPercent} per, dirty_count: {chunk_cache.dirtyCount}, mem_dirty: {chunk_cache.memDirty}")

    chunk_arr = None 
    dset_id = getDatasetId(chunk_id)
    dims = getChunkLayout(dset_json)
    type_json = dset_json["type"]
    layout = dset_json["layout"]
    dt = createDataType(type_json)

    bucket = None
    s3_offset = 0
    s3_size = None

    log.debug(f"getChunk dset_layout: {layout}")
    
    if layout["class"] == 'H5D_CONTIGUOUS_REF':
        if chunk_init:
            log.error(f"unable to initiale chunk {chunk_id} for H5D_CONTIGUOUS_REF layout ")
            raise  HTTPInternalServerError()
        if not layout["file_uri"].startswith("s3://"):
            log.error(f"layout file_uri is invalid: {layout['file_uri']}")
            raise HTTPInternalServerError()
        s3path = layout["file_uri"][5:]  # strip off the s3:// part
        index = s3path.find('/')   # split bucket and key
        if index < 1:
            log.error(f"s3path is invalid: {s3path}")
            raise HTTPInternalServerError()
        bucket = s3path[:index]
        s3_key = s3path[(index+1):]

        s3_offset = layout["offset"]
        s3_size = layout["size"]
        log.debug(f"getChunk range get - s3://{bucket}/{s3_key}  offset: {s3_offset} size: {s3_size} ")
    else:
        s3_key = getS3Key(chunk_id)
    log.debug("getChunk s3_key: {}".format(s3_key))
    if chunk_id in chunk_cache:
        chunk_arr = chunk_cache[chunk_id]
    else:
        obj_exists = await isS3Obj(app, s3_key, bucket=bucket)
        # TBD - potential race condition?
        if obj_exists:
            pending_s3_read = app["pending_s3_read"]
            if chunk_id in pending_s3_read:
                # already a read in progress, wait for it to complete
                read_start_time = pending_s3_read[chunk_id]
                log.info(f"s3 read request for {chunk_id} was requested at: {read_start_time}")
                while time.time() - read_start_time < 2.0:
                    log.debug("waiting for pending s3 read, sleeping")
                    await asyncio.sleep(1)  # sleep for sub-second?
                    if chunk_id in chunk_cache:
                        log.info(f"Chunk {chunk_id} has arrived!")
                        chunk_arr = chunk_cache[chunk_id]
                        break
                if chunk_arr is None:
                    log.warn(f"s3 read for chunk {chunk_id} timed-out, initiaiting a new read")
            
            if chunk_arr is None:
                if chunk_id not in pending_s3_read:
                    pending_s3_read[chunk_id] = time.time()
                log.debug("Reading chunk {} from S3".format(s3_key))
                deflate_level = getDeflate(app, dset_id, dset_json)
                chunk_bytes = await getS3Bytes(app, s3_key, deflate_level=deflate_level, s3_offset=s3_offset, s3_size=s3_size, bucket=bucket)
                if chunk_id in pending_s3_read:
                    # read complete - remove from pending map
                    elapsed_time = time.time() - pending_s3_read[chunk_id]
                    log.info(f"s3 read for {s3_key} took {elapsed_time}")
                    del pending_s3_read[chunk_id] 
                else:
                    log.warn(f"expected to find {chunk_id} in pending_s3_read map")

            
            chunk_arr = np.fromstring(chunk_bytes, dtype=dt)
            chunk_arr = chunk_arr.reshape(dims)

            log.debug("chunk size: {}".format(chunk_arr.size)) 
           
        elif chunk_init:
            log.debug("Initializing chunk {chunk_id}")
            fill_value = getFillValue(dset_json)
            if fill_value:
                # need to convert list to tuples for numpy broadcast
                if isinstance(fill_value, list):
                    fill_value = tuple(fill_value)
                chunk_arr = np.empty(dims, dtype=dt, order='C')
                chunk_arr[...] = fill_value
            else:
                chunk_arr = np.zeros(dims, dtype=dt, order='C')
        else:
            log.debug(f"Chunk {chunk_id} not found")
            
        if chunk_arr is not None:
            # check that there's room in the cache before adding it
            if chunk_cache.memTarget - chunk_cache.memDirty < chunk_arr.size:
                # no room in the cache, wait till space is freed by the s3sync task
                wait_start = time.time()
                while chunk_cache.memTarget - chunk_cache.memDirty < chunk_arr.size:
                    log.warn(f"getChunk, cache utilization: {chunk_cache.cacheUtilizationPercent}, sleeping till items are flushed")
                    if time.time() - wait_start > MAX_WAIT_TIME:
                        log.error(f"unable to save updated chunk {chunk_id} to cache returning 503 error")
                        raise HTTPServiceUnavailable()
                    await asyncio.sleep(1)
                 
            chunk_cache[chunk_id] = chunk_arr  # store in cache
    return chunk_arr

"""
Update the requested chunk/selection
"""
async def PUT_Chunk(request):
    log.request(request)
    app = request.app 
    params = request.rel_url.query
  
    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
  
    if not request.has_body:
        msg = "PUT Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    content_type = "application/octet-stream"
    if "Content-Type" in request.headers:
        # client should use "application/octet-stream" for binary transfer
        content_type = request.headers["Content-Type"]
    if content_type != "application/octet-stream":
        msg = "Unexpected content_type: {}".format(content_type)
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(params.keys())))
    if "dset" not in params:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    dset_json = json.loads(params["dset"])
    log.debug("dset_json: {}".format(dset_json))

    dims = getChunkLayout(dset_json)
   
    if "root" not in dset_json:
        msg = "expected root key in dset_json"
        log.error(msg)
        raise KeyError(msg)
    
    rank = len(dims)  
     
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
        raise HTTPBadRequest(reason=msg)
    if len(selection) != rank:
        msg = "Selection rank does not match shape rank"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    for i in range(rank):
        s = selection[i]
        log.debug("selection[{}]: {}".format(i, s))

    mshape = getSelectionShape(selection)
    log.debug(f"mshape: {mshape}")
    num_elements = 1
    for extent in mshape:
        num_elements *= extent
        
    # check that the content_length is what we expect
    if itemsize != 'H5T_VARIABLE':
        log.debug("expect content_length: {}".format(num_elements*itemsize))
    log.debug("actual content_length: {}".format(request.content_length))

    if itemsize != 'H5T_VARIABLE' and (num_elements * itemsize) != request.content_length:
        msg = "Expected content_length of: {}, but got: {}".format(num_elements*itemsize, request.content_length)
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

     # create a numpy array for incoming data
    input_bytes = await request_read(request)  # TBD - will it cause problems when failures are raised before reading data?
    if len(input_bytes) != request.content_length:
        msg = "Read {} bytes, expecting: {}".format(len(input_bytes), request.content_length)
        log.error(msg)
        raise HTTPInternalServerError()
        
    input_arr = bytesToArray(input_bytes, dt, mshape)

    chunk_arr = await getChunk(app, chunk_id, dset_json, chunk_init=True)

    # update chunk array
    chunk_arr[selection] = input_arr
    chunk_cache = app["chunk_cache"]
    chunk_cache.setDirty(chunk_id)
    log.info(f"PUT_Chunk dirty cache count: {chunk_cache.dirtyCount}")

    # async write to S3   
    dirty_ids = app["dirty_ids"]
    now = int(time.time())
    dirty_ids[chunk_id] = now
    
    # chunk update successful     
    resp = json_response({}, status=201)
    log.response(request, resp=resp)
    return resp


"""
Return data from requested chunk and selection
"""
async def GET_Chunk(request):
    log.request(request)
    app = request.app 
    params = request.rel_url.query

    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(params.keys())))
    if "dset" not in params:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    dset_json = json.loads(params["dset"])
    
    log.debug("dset_json: {}".format(dset_json)) 
    type_json = dset_json["type"]
     
    dims = getChunkLayout(dset_json)
    log.debug("got dims: {}".format(dims))
    rank = len(dims)  
         
    # get chunk selection from query params
    if "select" in params:
        log.debug("select: {}".format(params["select"]))
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
        raise HTTPBadRequest(reason=msg)
    if len(selection) != rank:
        msg = "Selection rank does not match shape rank"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    for i in range(rank):
        s = selection[i]
        log.debug("selection[{}]: {}".format(i, s))

    chunk_arr = await getChunk(app, chunk_id, dset_json)

    if chunk_arr is None:
        # return a 404
        msg = "Chunk {} does not exist".format(chunk_id)
        log.info(msg)
        raise HTTPNotFound()
     
    resp = None
    
    if "query" in params:
        # do query selection
        query = params["query"]
        log.info("query: {}".format(query))
        if rank != 1:
            msg = "Query selection only supported for one dimensional arrays"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        limit = 0
        if "Limit" in params:
            limit = int(params["Limit"])

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
        log.info(f"query_result retiurning: {len(indices)} rows")
        log.debug(f"query_result: {query_result}")
        resp = json_response(query_result)
    else:
        # get requested data
        output_arr = chunk_arr[selection]
        output_data = arrayToBytes(output_arr)

        # write response
        try:
            resp = StreamResponse()
            resp.headers['Content-Type'] = "application/octet-stream"
            resp.content_length = len(output_data)
            await resp.prepare(request)
            await resp.write(output_data)
        except Exception as e:
            log.error(f"Exception during binary data write: {e}")
            raise HTTPInternalServerError()

        finally:
            await resp.write_eof()

    return resp

"""
Return data from requested chunk and point selection
"""
async def POST_Chunk(request):
    log.request(request)
    app = request.app 
    params = request.rel_url.query

    put_points = False
    num_points = 0
    if "count" in params:
        num_points = int(params["count"])

    if "action" in params and params["action"] == "put":
        log.info("POST Chunk put points, num_points: {}".format(num_points))

        put_points = True
    else:
        log.info("POST Chunk get points")

    chunk_id = request.match_info.get('id')
    if not chunk_id:
        msg = "Missing chunk id"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    log.info("POST chunk_id: {}".format(chunk_id))
    chunk_index = getChunkIndex(chunk_id)
    log.debug("chunk_index: {}".format(chunk_index))
    
    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    validateInPartition(app, chunk_id)
    log.debug("request params: {}".format(list(params.keys())))
    if "dset" not in params:
        msg = "Missing dset in GET request"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
    dset_json = json.loads(params["dset"])
    log.debug("dset_json: {}".format(dset_json))
    chunk_layout = getChunkLayout(dset_json)
    chunk_coord = getChunkCoordinate(chunk_id, chunk_layout)
    log.debug("chunk_coord: {}".format(chunk_coord))
    
    if not request.has_body:
        msg = "POST Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    content_type = "application/octet-stream"
    if "Content-Type" in request.headers:
        # client should use "application/octet-stream" for binary transfer
        content_type = request.headers["Content-Type"]
    if content_type != "application/octet-stream":
        msg = "Unexpected content_type: {}".format(content_type)
        log.error(msg)
        raise HTTPBadRequest(reason=msg)
     
    type_json = dset_json["type"]
    dset_dtype = createDataType(type_json)
    log.debug("dtype: {}".format(dset_dtype))

    dims = getChunkLayout(dset_json)
    log.debug("got dims: {}".format(dims))
    rank = len(dims)
    if rank == 0:
        msg = "POST chunk request with no dimensions"
        log.error(msg)
        raise HTTPBadRequest(reason=msg)

    # create a numpy array for incoming points
    input_bytes = await request_read(request) 
    if len(input_bytes) != request.content_length:
        msg = "Read {} bytes, expecting: {}".format(len(input_bytes), request.content_length)
        log.error(msg)
        raise HTTPInternalServerError()

    # get chunk from cache/s3.  If not found init a new chunk if this is a write request
    chunk_arr = await getChunk(app, chunk_id, dset_json, chunk_init=put_points) 

   
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
            raise HTTPBadRequest(reason=msg)
        for i in range(num_points):
            elem = point_arr[i]
            if rank == 1:
                coord = int(elem[0])
            else:
                coord = tuple(elem[0]) # index to update
            val = elem[1]   # value 
            chunk_arr[coord] = val # update the point

        chunk_cache = app["chunk_cache"]
        chunk_cache.setDirty(chunk_id)

        # async write to S3   
        dirty_ids = app["dirty_ids"]
        now = int(time.time())
        dirty_ids[chunk_id] = now
        log.info("set {} to dirty".format(chunk_id))
    
    else:
        # reading point data  
        point_dt = np.dtype('uint64')  # use unsigned long for point index  
        point_arr = np.fromstring(input_bytes, dtype=point_dt)  # read points as unsigned longs
        if len(point_arr) % rank != 0:
            msg = "Unexpected size of point array"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
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
        resp = json_response({})
    else:
        # get data
        output_data = output_arr.tobytes()

        # write response
        try:
            resp = StreamResponse()
            resp.headers['Content-Type'] = "application/octet-stream"
            resp.content_length = len(output_data)
            await resp.prepare(request)
            await resp.write(output_data)
        except Exception as e:
            log.error(f"Exception during binary data write: {e}")
            raise HTTPInternalServerError()
        finally:
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
        raise HTTPBadRequest(reason=msg)
    log.info("DELETE chunk: {}".format(chunk_id))

    if not isValidUuid(chunk_id, "Chunk"):
        msg = "Invalid chunk id: {}".format(chunk_id)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    validateInPartition(app, chunk_id)

    chunk_cache = app['chunk_cache'] 
    s3_key = getS3Key(chunk_id)
    log.debug("DELETE_Chunk s3_key: {}".format(s3_key))

    if chunk_id in chunk_cache:
        del chunk_cache[chunk_id]

    deflate_map = app["deflate_map"]
    dset_id = getDatasetId(chunk_id)
    if dset_id in deflate_map:
        # The only reason chunks are ever deleted is if the dataset is being deleted,
        # so it should be save to remove this entry now
        log.info("Removing deflate_map entry for {}".format(dset_id))
        del deflate_map[dset_id]

    resp_json = {  }   
    resp = json_response(resp_json)
    log.response(request, resp=resp)
    return resp
