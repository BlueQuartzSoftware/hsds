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
import asyncio
import json
import time
import sys

from aiohttp.web import Application, Response, StreamResponse, run_app
from aiohttp import ClientSession, TCPConnector, HttpProcessingError 
from aiohttp.errors import HttpBadRequest, ClientOSError
from botocore.exceptions import ClientError
 

import config
from util.timeUtil import unixTimeToUTC, elapsedTime
from util.idUtil import createNodeId, createObjId, getObjPartition, getS3Key, validateUuid
from util.httpUtil import isOK, http_post, jsonResponse
from util.s3Util import getS3JSONObj, putS3JSONObj, isS3Obj 
from util.domainUtil import getS3KeyForDomain, validateDomain
from basenode import register, healthCheck, info, baseInit
import hsds_logger as log

async def GET_Domain(request):
    """HTTP GET method to return JSON for /domains/
    """
    log.request(request)
    app = request.app
    domain_key = request.match_info.get('key')
    log.info("domain: {}".format(domain_key))
    s3_key = None
    try:
        s3_key = getS3KeyForDomain(domain_key)
        log.info("s3_key for domain {}: {}".format(domain_key, s3_key))
    except ValueError as ve:
        msg = "Invalid domain key: {}".format(str(ve))
        log.warn(msg)
        raise HttpBadRequest(msg)

    if getObjPartition(s3_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpBadRequest(message="wrong node for 'key':{}".format(s3_key))

    meta_cache = app['meta_cache'] 
    domain_json = None 
    if s3_key in meta_cache:
        log.info("{} found in meta cache".format(s3_key))
        domain_json = meta_cache[s3_key]
    else:
        try:
            log.info("getS3JSONObj({})".format(s3_key))
            domain_json = await getS3JSONObj(app, s3_key)
        except ClientError as ce:
            # key does not exist?
            log.warn("got ClientError on s3 get: {}".format(str(ce)))
            is_s3obj = await isS3Obj(app, s3_key)
            if is_s3obj:
                msg = "Error getting s3 obj: " + str(ce)
                log.response(request, code=500, message=msg)
                raise HttpProcessingError(code=500, message=msg)
            else:
                msg = "{} not found".format(s3_key)
                log.response(request, code=404, message=msg)
                raise HttpProcessingError(code=404, message=msg)
        meta_cache[s3_key] = domain_json

    resp = await jsonResponse(request, domain_json)
    log.response(request, resp=resp)
    return resp

async def PUT_Domain(request):
    """HTTP PUT method to create a domain
    """
    log.request(request)
    app = request.app
    domain_key = request.match_info.get('key')
    log.info("domain: {}".format(domain_key))
    s3_key = None
    try:
        s3_key = getS3KeyForDomain(domain_key)
        log.info("s3_key for domain {}: {}".format(domain_key, s3_key))
    except ValueError as ve:
        msg = "Invalid domain key: {}".format(str(ve))
        log.warn(msg)
        raise HttpBadRequest(msg)

    if getObjPartition(s3_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpBadRequest(message="wrong node for 'key':{}".format(s3_key))

    meta_cache = app['meta_cache'] 
    
    domain_exist = False
    if s3_key in meta_cache:
        log.info("{} found in meta cache".format(s3_key))
        domain_exist = True
    else:
        domain_exist = await isS3Obj(app, s3_key)
    if domain_exist:
        # this domain already exists, client must delete it first
        msg = "Conflict: resource exists: " + domain_key
        log.info(msg)
        raise HttpProcessingError(code=409, message=msg)   

    if not request.has_body:
        msg = "Expected Body to be in request"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 

    body_json = await request.json()
    if "owner" not in body_json:
        msg = "Expected Owner Key in Body"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 
    if "acls" not in body_json:
        msg = "Expected Owner Key in Body"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 
    if "root" not in body_json:
        msg = "Expected root Key in Body"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 

     
    domain_json = { }
    domain_json["root"] = body_json["root"]
    domain_json["owner"] = body_json["owner"]
    domain_json["acls"] = body_json["acls"]

    try:
        await putS3JSONObj(app, s3_key, domain_json)  # write to S3
    except ClientError as ce:
        msg = "Error writing s3 obj: " + str(ce)
        log.response(request, code=500, message=msg)
        raise HttpProcessingError(code=500, message=msg)

    # read back from S3 (will add timestamps metakeys) 
    log.info("getS3JSONObj({})".format(s3_key))
    try:
        domain_json = await getS3JSONObj(app, s3_key)
    except ClientError as ce:
        msg = "Error reading s3 obj: " + s3_key
        log.response(request, code=500, message=msg)
        raise HttpProcessingError(code=500, message=msg)
    meta_cache[s3_key] = domain_json

    resp = await jsonResponse(request, domain_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Domain(request):
    """HTTP DELETE method to delete a domain
    """
    log.request(request)
    app = request.app
    domain_key = request.match_info.get('key')
    log.info("domain: {}".format(domain_key))
    s3_key = None
    try:
        s3_key = getS3KeyForDomain(domain_key)
        log.info("s3_key for domain {}: {}".format(domain_key, s3_key))
    except ValueError as ve:
        msg = "Invalid domain key: {}".format(str(ve))
        log.warn(msg)
        raise HttpBadRequest(msg)

    if getObjPartition(s3_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpBadRequest(message="wrong node for 'key':{}".format(s3_key))

    meta_cache = app['meta_cache'] 
    
    domain_exist = False
    if s3_key in meta_cache:
        log.info("{} found in meta cache".format(s3_key))
        domain_exist = True
    else:
        domain_exist = await isS3Obj(app, s3_key)
    if not domain_exist:
        # the domain is not found, return a 404
        msg = "Domain {} not found".format(domain_key)
        log.info(msg)
        raise HttpProcessingError(code=404, message=msg)   

    json_response = { "domain": domain_key }

    resp = await jsonResponse(request, json_response, status=200)
    log.response(request, resp=resp)
    return resp

   