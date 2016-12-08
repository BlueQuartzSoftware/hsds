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
# handles dataset /value requests
# 
import asyncio
import json
import base64 
import numpy as np
from aiohttp.errors import HttpBadRequest, HttpProcessingError, ClientError
from aiohttp.web import StreamResponse
from util.httpUtil import  jsonResponse, getHref, getAcceptType  
from util.idUtil import   isValidUuid, getDataNodeUrl
from util.domainUtil import  getDomainFromRequest, isValidDomain
from util.hdf5dtype import getItemSize, createDataType
from util.dsetUtil import getSliceQueryParam, setSliceQueryParam, getChunkLayout 
from util.dsetUtil import getSelectionShape, getNumElements, getDsetDims
from util.chunkUtil import getNumChunks, getChunkIds
from util.chunkUtil import getChunkCoverage, getDataCoverage
from util.arrayUtil import bytesArrayToList, toTuple 
from util.authUtil import getUserPasswordFromRequest, validateUserPassword
from servicenode_lib import getObjectJson, validateAction
import config
import hsds_logger as log


"""
Write data to given chunk_id.  Pass in type, dims, and selection area.
"""
async def write_chunk_hyperslab(app, chunk_id, dset_json, slices, arr):
    """ write the chunk selection to the DN
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to write to
    np_arr: numpy array of data to be written
    """
    log.info("write_chunk_hyperslab, chunk_id:{}, slices:{}".format(chunk_id, slices))
    if "layout" not in dset_json:
        log.error("No layout found in dset_json: {}".format(dset_json))
        raise HttpProcessingError(message="Unexpected error", code=500)
     
    if "type" not in dset_json:
        log.error("No type found in dset_json: {}".format(dset_json))
        raise HttpProcessingError(message="Unexpected error", code=500)
    type_json = dset_json["type"]
    dims = getDsetDims(dset_json)
    layout = getChunkLayout(dset_json)

    chunk_sel = getChunkCoverage(chunk_id, slices, layout)
    log.info("chunk_sel: {}".format(chunk_sel))
    data_sel = getDataCoverage(chunk_id, slices, layout)
    log.info("data_sel: {}".format(data_sel))
    log.info("arr.shape: {}".format(arr.shape))
    arr_chunk = arr[data_sel]
    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id 
    log.info("PUT chunk req: " + req)
    client = app['client']
    data = arr_chunk.tobytes()  # TBD - this makes a copy, use np_arr.data to get memoryview and avoid copy
    # pass itemsize, type, dimensions, and selection as query params
    params = {}
    params["itemsize"] = str(arr.itemsize)
    params["type"] = json.dumps(type_json)
    setSliceQueryParam(params, dims, chunk_sel)   

    try:
        async with client.put(req, data=data, params=params) as rsp:
            log.info("req: {} status: {}".format(req, rsp.status))
            if rsp.status != 201:
                msg = "request error for {}: {}".format(req, str(rsp))
                log.warn(msg)
                raise HttpProcessingError(message=msg, code=rsp.status)
            else:
                log.info("http_put({}) <201> Updated".format(req))
    except ClientError as ce:
        log.error("Error for http_post({}): {} ".format(req, str(ce)))
        raise HttpProcessingError(message="Unexpected error", code=500)


"""
Read data from given chunk_id.  Pass in type, dims, and selection area.
""" 
async def read_chunk_hyperslab(app, chunk_id, dset_json, slices, np_arr):
    """ read the chunk selection from the DN
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to read from
    np_arr: numpy array to store read bytes
    """
    msg = "read_chunk_hyperslab, chunk_id:{}, slices: {}".format(chunk_id, slices)
    log.info(msg)

    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id 
    log.info("GET chunk req: " + req)
    client = app['client']

    if "type" not in dset_json:
        log.error("No type found in dset_json: {}".format(dset_json))
        raise HttpProcessingError(message="Unexpected error", code=500)
    type_json = dset_json["type"]
    dims = getDsetDims(dset_json)
    layout = getChunkLayout(dset_json)
    chunk_sel = getChunkCoverage(chunk_id, slices, layout)
    data_sel = getDataCoverage(chunk_id, slices, layout)
    
    # pass itemsize, type, dimensions, and selection as query params
    params = {}
    params["itemsize"] = str(np_arr.itemsize)
    params["type"] = json.dumps(type_json)
    chunk_shape = getSelectionShape(chunk_sel)
    log.info("chunk_shape: {}".format(chunk_shape))
    setSliceQueryParam(params, dims, chunk_sel)  
    dt = np_arr.dtype
 
    chunk_arr = None
    try:
        async with client.get(req, params=params) as rsp:
            log.info("http_get {} status: <{}>".format(req, rsp.status))
            if rsp.status == 200:
                data = await rsp.read()  # read response as bytes
                chunk_arr = np.fromstring(data, dtype=dt) 
                npoints_read = getNumElements(chunk_arr.shape)
                npoints_expected = getNumElements(chunk_shape)
                if npoints_read != npoints_expected:
                    msg = "Expected {} points, but got: {}".format(npoints_expected, npoints_read)
                chunk_arr = chunk_arr.reshape(chunk_shape)
            elif rsp.status == 404:
                # no data, return zero array
                # TBD - use fill value
                chunk_arr = np.zeros(chunk_shape, dtype=dt)
            else:
                msg = "request to {} failed with code: {}".format(req, rsp.status)
                log.warn(msg)
                raise HttpProcessingError(message=msg, code=rsp.status)
            
    except ClientError as ce:
        log.error("Error for http_get({}): {} ".format(req, str(ce)))
        raise HttpProcessingError(message="Unexpected error", code=500)
    
    log.info("chunk_arr shape: {}".format(chunk_arr.shape))
    log.info("data_sel: {}".format(data_sel))

    np_arr[data_sel] = chunk_arr
    


"""
 Handler for PUT /<dset_uuid>/value request
"""
async def PUT_Value(request):
    log.request(request)
    app = request.app 
    loop = app["loop"]
    body = None
    json_data = None

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    username, pswd = getUserPasswordFromRequest(request)
    validateUserPassword(username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    request_type = "json"
    if "Content-Type" in request.headers:
        # client should use "application/octet-stream" for binary transfer
        content_type = request.headers["Content-Type"]
        if content_type not in ("application/json", "application/octet-stream"):
            msg = "Unknown content_type: {}".format(content_type)
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        if content_type == "application/octet-stream":
            log.info("request_type is binary")
            request_type = "binary"

    if not request.has_body:
        msg = "PUT Value with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    log.info("request_type: {}".format(request_type))
    if request_type == "json":
        body = await request.json()
    
    # get  state for dataset from DN.
    dset_json = await getObjectJson(app, dset_id)  

    layout = None 
    datashape = dset_json["shape"]
    if datashape["class"] == 'H5S_NULL':
        msg = "Null space datasets can not be used as target for PUT value"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    dims = getDsetDims(dset_json)
    rank = len(dims)
    layout = getChunkLayout(dset_json)
     
    type_json = dset_json["type"]
    item_size = getItemSize(type_json)
    if item_size == 'H5T_VARIABLE' and request_type != "json":
        msg = "Only JSON is supported for variable length data types"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    dset_dtype = createDataType(type_json)  # np datatype
    
    await validateAction(app, domain, dset_id, username, "update")
 
    binary_data = None
    slices = []  # selection for write 
    
    # Get query parameter for selection
    for dim in range(rank):
        body_json = None
        if request_type == "json":
            body_json = body
        dim_slice = getSliceQueryParam(request, dim, dims[dim], body=body_json)
        slices.append(dim_slice)   
    slices = tuple(slices)  
    log.info("PUT Value selection: {}".format(slices))   
     
    log.info("item size: {}".format(item_size))

    np_shape = getSelectionShape(slices)
                 
    log.info("selection shape:" + str(np_shape))

    npoints = getNumElements(np_shape)
    log.info("selection num points: " + str(npoints))
    if npoints <= 0:
        msg = "Selection is empty"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    if request_type == "json":
        if "value" in body:
            json_data = body["value"]
        elif "value_base64" in body:
            base64_data = body["value_base64"]
            base64_data = base64_data.encode("ascii")
            binary_data = base64.b64decode(base64_data)
        else:
            msg = "PUT value has no value or value_base64 key in body"
            log.warn(msg)
            raise HttpBadRequest(message=msg)   
    else:
        # read binary data
        binary_data = await request.read()
        if len(binary_data) != request.content_length:
            msg = "Read {} bytes, expecting: {}".format(len(binary_data), request.content_length)
            log.error(msg)
            raise HttpProcessingError(code=500, message="Unexpected Error")

    arr = None  # np array to hold request data
    if binary_data:
        if npoints*item_size != len(binary_data):
            msg = "Expected: " + str(npoints*item_size) + " bytes, but got: " + str(len(binary_data))
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        arr = np.fromstring(binary_data, dtype=dset_dtype)
        arr = arr.reshape(np_shape)  # conform to selection shape
                
    else:
        # data is json

        # need some special conversion for compound types --
        # each element must be a tuple, but the JSON decoder
        # gives us a list instead.
        if len(dset_dtype) > 1 and type(json_data) in (list, tuple):
            np_shape_rank = len(np_shape)
            #log.info("np_shape_rank: {}".format(np_shape_rank))
            converted_data = []
            if npoints == 1:
                converted_data.append(toTuple(0, json_data))
            else:  
                converted_data = toTuple(np_shape_rank, json_data)
            json_data = converted_data

        log.info("json_data: {}".format(json_data))
        arr = np.array(json_data, dtype=dset_dtype)
        # raise an exception of the array shape doesn't match the selection shape
        # allow if the array is a scalar and the selection shape is one element,
        # numpy is ok with this
        if arr.size != npoints:
            msg = "Input data doesn't match selection number of elements"
            msg += " Expected {}, but received: {}".format(npoints, arr.size)
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        if arr.shape != np_shape:
            arr = arr.reshape(np_shape)  # reshape to match selection

        np_index = 0
        for dim in range(len(arr.shape)):
            data_extent = arr.shape[dim]
            selection_extent = 1
            if np_index < len(np_shape):
                selection_extent = np_shape[np_index]
            if selection_extent == data_extent:
                np_index += 1
                continue  # good
            if data_extent == 1:
                continue  # skip singleton selection
            if selection_extent == 1:
                np_index += 1
                continue  # skip singleton selection
                 
            # selection/data mismatch!
            msg = "data shape doesn't match selection shape"
            msg += "--data shape: " + str(arr.shape)
            msg += "--selection shape: " + str(np_shape)         
            log.warn(msg)
            raise HttpBadRequest(message=msg)

    #log.info("got np array: {}".format(arr))
    num_chunks = getNumChunks(slices, layout)
    log.info("num_chunks: {}".format(num_chunks))
    if num_chunks > config.get("max_chunks_per_request"):
        msg = "PUT value request too large"
        log.warn(msg)
        raise HttpProcessingError(code=413, message=msg)

    chunk_ids = getChunkIds(dset_id, slices, layout)
    log.info("chunk_ids: {}".format(chunk_ids))

    tasks = []
    for chunk_id in chunk_ids:
        task = asyncio.ensure_future(write_chunk_hyperslab(app, chunk_id, dset_json, slices, arr))
        tasks.append(task)
    await asyncio.gather(*tasks, loop=loop)

    resp_json = {}
    resp_json["hrefs"] = []  # TBD

    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp

"""
 Handler for GET /<dset_uuid>/value request
"""
async def GET_Value(request):
    log.request(request)
    app = request.app 
    loop = app["loop"]

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = "Invalid dataset id: {}".format(dset_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(username, pswd)
     
    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    accept_type = getAcceptType(request)
    response_type = accept_type # will adjust later if binary not possible
   
    # get  state for dataset from DN.
    dset_json = await getObjectJson(app, dset_id)  

    datashape = dset_json["shape"]
    if datashape["class"] == 'H5S_NULL':
        msg = "GET value not supported for datasets with NULL shape"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    dims = getDsetDims(dset_json)
    rank = len(dims)
    layout = getChunkLayout(dset_json)
     
    type_json = dset_json["type"]
    item_size = getItemSize(type_json)
    log.info("item size: {}".format(item_size))
    dset_dtype = createDataType(type_json)  # np datatype

    log.info("got dset_json: {}".format(dset_json))
    await validateAction(app, domain, dset_id, username, "read")

    if item_size == 'H5T_VARIABLE' and accept_type != "json":
        msg = "Client requested binary, but only JSON is supported for variable length data types"
        log.info(msg)
        response_type = "json"

    slices = []  # selection for read 
     
    # Get query parameter for selection
    for dim in range(rank):
        dim_slice = getSliceQueryParam(request, dim, dims[dim])
        slices.append(dim_slice)   

    slices = tuple(slices)  
    log.info("GET Value selection: {}".format(slices))   
     
    np_shape = getSelectionShape(slices)                
    log.info("selection shape:" + str(np_shape))

    npoints = getNumElements(np_shape)
    log.info("selection num points: " + str(npoints))

    num_chunks = getNumChunks(slices, layout)
    log.info("num_chunks: {}".format(num_chunks))
    if num_chunks > config.get("max_chunks_per_request"):
        msg = "PUT value request too large"
        log.warn(msg)
        raise HttpProcessingError(code=413, message=msg)

    chunk_ids = getChunkIds(dset_id, slices, layout)
    log.info("chunk_ids: {}".format(chunk_ids))

    # create array to hold response data
    # TBD: initialize to fill value if not 0
    arr = np.zeros(np_shape, dtype=dset_dtype)
    tasks = []
    for chunk_id in chunk_ids:
        task = asyncio.ensure_future(read_chunk_hyperslab(app, chunk_id, dset_json, slices, arr))
        tasks.append(task)
    await asyncio.gather(*tasks, loop=loop)

    log.info("arr shape: {}".format(arr.shape))

    if response_type == "binary":
        output_data = arr.tobytes()
     
        # write response
        resp = StreamResponse(status=200)
        resp.headers['Content-Type'] = "application/octet-stream"
        resp.content_length = len(output_data)
        await resp.prepare(request)
        resp.write(output_data)
        await resp.write_eof()
    else:
        resp_json = {}
        data = arr.tolist()
        json_data = bytesArrayToList(data)
        if datashape["class"] == 'H5S_SCALAR':
            # convert array response to value
            resp_json["value"] = json_data[0]
        else:
            resp_json["value"] = json_data  
    
        hrefs = []
        dset_uri = '/datasets/'+dset_id
        hrefs.append({'rel': 'self', 'href': getHref(request, dset_uri + '/value')})
        root_uri = '/groups/' + dset_json["root"]    
        hrefs.append({'rel': 'root', 'href': getHref(request, root_uri)})
        hrefs.append({'rel': 'home', 'href': getHref(request, '/')})
        hrefs.append({'rel': 'owner', 'href': getHref(request, dset_uri)})
        resp_json["hrefs"] = hrefs
 
        resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp




