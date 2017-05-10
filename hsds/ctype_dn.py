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
# data node of hsds cluster
# 
import time

from aiohttp.errors import HttpBadRequest, HttpProcessingError
 
from util.idUtil import isValidUuid, validateUuid
from util.httpUtil import jsonResponse
from util.domainUtil import validateDomain
from datanode_lib import get_obj_id, get_metadata_obj, save_metadata_obj, delete_metadata_obj, check_metadata_obj
import hsds_logger as log
    

async def GET_Datatype(request):
    """HTTP GET method to return JSON for /groups/
    """
    log.request(request)
    app = request.app
    ctype_id = get_obj_id(request)  
    
    if not isValidUuid(ctype_id, obj_class="type"):
        log.error( "Unexpected type_id: {}".format(ctype_id))
        raise HttpProcessingError(code=500, message="Unexpected Error")
    
    ctype_json = await get_metadata_obj(app, ctype_id)

    resp_json = { } 
    resp_json["id"] = ctype_json["id"]
    resp_json["root"] = ctype_json["root"]
    resp_json["created"] = ctype_json["created"]
    resp_json["lastModified"] = ctype_json["lastModified"]
    resp_json["type"] = ctype_json["type"]
    resp_json["attributeCount"] = len(ctype_json["attributes"])
    resp_json["domain"] = ctype_json["domain"]
     
    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp

async def POST_Datatype(request):
    """ Handler for POST /datatypes"""
    log.info("Post_Datatype")
    log.request(request)
    app = request.app

    if not request.has_body:
        msg = "POST_Datatype with no body"
        log.error(msg)
        raise HttpBadRequest(message=msg)

    body = await request.json()
    
    ctype_id = get_obj_id(request, body=body)
    if not isValidUuid(ctype_id, obj_class="datatype"):
        log.error( "Unexpected type_id: {}".format(ctype_id))
        raise HttpProcessingError(code=500, message="Unexpected Error")

    try:
        # verify the id doesn't already exist
        await check_metadata_obj(app, ctype_id)
        log.error( "Post with existing type_id: {}".format(ctype_id))
        raise HttpProcessingError(code=500, message="Unexpected Error")
    except HttpProcessingError:
        pass  # expected

    root_id = None
    domain = None
    
    if "root" not in body:
        msg = "POST_Datatype with no root"
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")
    root_id = body["root"]
    try:
        validateUuid(root_id, "group")
    except ValueError:
        msg = "Invalid root_id: " + root_id
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")
     
    if "type" not in body:
        msg = "POST_Datatype with no type"
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")
    type_json = body["type"]
     
    if "domain" not in body:
        msg = "POST_Datatype with no domain key"
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")

    domain = body["domain"]
    try:
        validateDomain(domain)
    except ValueError:
        msg = "Invalid domain: " + domain
        log.error(msg)
        raise HttpProcessingError(code=500, message="Unexpected Error")

    # ok - all set, create committed type obj
    now = time.time()

    log.info("POST_datatype, typejson: {}". format(type_json))
    
    ctype_json = {"id": ctype_id, "root": root_id, "created": now, 
        "lastModified": now, "type": type_json, "attributes": {}, "domain": domain }
     
    save_metadata_obj(app, ctype_id, ctype_json)

    resp_json = {} 
    resp_json["id"] = ctype_id 
    resp_json["root"] = root_id
    resp_json["created"] = ctype_json["created"]
    resp_json["lastModified"] = ctype_json["lastModified"]
    resp_json["attributeCount"] = 0

    resp = await jsonResponse(request, resp_json, status=201)
    log.response(request, resp=resp)
    return resp


async def DELETE_Datatype(request):
    """HTTP DELETE method for datatype
    """
    log.request(request)
    app = request.app
    
    ctype_id = get_obj_id(request)
    log.info("DELETE ctype: {}".format(ctype_id))

    # verify the id  exist
    await check_metadata_obj(app, ctype_id)
        
    log.info("deleting ctype: {}".format(ctype_id))

    notify=True
    if "Notify" in request.GET and not request.GET["Notify"]:
        notify=False
    await delete_metadata_obj(app, ctype_id, notify=notify)
 
    resp_json = {  } 
      
    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp
   
