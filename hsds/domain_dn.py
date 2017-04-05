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
from util.idUtil import   getObjPartition
from util.authUtil import  getAclKeys
from util.httpUtil import  jsonResponse
from util.s3Util import getS3JSONObj, putS3JSONObj, isS3Obj, deleteS3Obj
from util.domainUtil import getS3KeyForDomain, validateDomainKey
import hsds_logger as log

async def GET_Domain(request):
    """HTTP GET method to return JSON for /domains/
    """
    log.request(request)
    app = request.app

    domain = None
    if "domain" in request.GET:
        domain = request.GET["domain"]
        log.info("got domain param: {}".format(domain))
    else: 
        msg = "No domain provided"  
        log.error(msg)
        raise HttpProcessingError(code=500, message=msg) 

    domain_key = getS3KeyForDomain(domain)
    try:
        validateDomainKey(domain_key)
    except ValueError:
        msg = "Invalid domain key"
        log.error(msg)
        raise HttpProcessingError(code=500, message=msg) 

    log.info("s3 domain key: {}".format(domain_key))

    if getObjPartition(domain_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        msg = "wrong node for domain: {}".format(domain_key)
        log.error(msg)
        raise HttpProcessingError(code=500, message=msg) 

    meta_cache = app['meta_cache'] 
    deleted_ids = app['deleted_ids']

    if domain_key in deleted_ids:
        msg = "Domain: {} has been deleted".format(domain_key)
        log.warn(msg)
        raise HttpProcessingError(code=410, message=msg)

    domain_json = None 
    if domain_key in meta_cache:
        log.info("{} found in meta cache".format(domain_key))
        domain_json = meta_cache[domain_key]
    else:      
        domain_json = await getS3JSONObj(app, domain_key)  
        meta_cache[domain_key] = domain_json  # save to cache

    resp = await jsonResponse(request, domain_json)
    log.response(request, resp=resp)
    return resp

async def PUT_Domain(request):
    """HTTP PUT method to create a domain
    """
    log.request(request)
    app = request.app

    if not request.has_body:
        msg = "Expected Body to be in request"
        log.error(msg)
        raise HttpProcessingError(code=500, message=msg) 

    body_json = await request.json()

    if "domain" not in body_json:
        msg = "Missing domain"
        log.error(msg)
        raise HttpProcessingError(code=500, message=msg) 

    domain = body_json["domain"]
    log.info("domain: {}".format(domain))
     
    domain_key = getS3KeyForDomain(domain)
    log.info("s3 domain key: {}".format(domain))

    if getObjPartition(domain_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpBadRequest(message="wrong node for 'key':{}".format(domain))

    meta_cache = app['meta_cache'] 
    deleted_ids = app['deleted_ids']
    
    domain_exist = False
    if domain_key in meta_cache:
        log.info("{} found in meta cache".format(domain_key))
        domain_exist = True
    else:
        domain_exist = await isS3Obj(app, domain_key)
    if domain_exist:
        # this domain already exists, client must delete it first
        msg = "Conflict: resource exists: " + domain
        log.info(msg)
        raise HttpProcessingError(code=409, message=msg)   
    
    if "owner" not in body_json:
        msg = "Expected Owner Key in Body"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 
    if "acls" not in body_json:
        msg = "Expected Owner Key in Body"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 
       
    domain_json = { }
    if "root" in body_json:
        domain_json["root"] = body_json["root"]
    else:
        log.info("no root id, creating folder")
    domain_json["owner"] = body_json["owner"]
    domain_json["acls"] = body_json["acls"]
    now = time.time()
    domain_json["created"] = now
    domain_json["lastModified"] = now

    # write to S3
    await putS3JSONObj(app, domain_key, domain_json)  
     
    # read back from S3 (will add timestamps metakeys) 
    log.info("getS3JSONObj({})".format(domain_key))
    domain_json = await getS3JSONObj(app, domain_key)
     
    meta_cache[domain_key] = domain_json
    if domain_key in deleted_ids:
        deleted_ids.remove(domain_key)  # un-gone the domain key

    resp = await jsonResponse(request, domain_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Domain(request):
    """HTTP DELETE method to delete a domain
    """
    log.request(request)
    app = request.app

    if not request.has_body:
        msg = "Expected Body to be in request"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 

    body_json = await request.json()

    if "domain" not in body_json:
        msg = "Missing domain"
        log.warn(msg)
        raise HttpBadRequest(msg)

    domain = body_json["domain"]
    log.info("domain: {}".format(domain))

    domain_key = getS3KeyForDomain(domain)
    log.info("domain key: {}".format(domain_key))

    if getObjPartition(domain_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpBadRequest(message="wrong node for 'key':{}".format(domain_key))

    meta_cache = app['meta_cache'] 
    deleted_ids = app['deleted_ids']
    
    domain_exist = False
    if domain_key in meta_cache:
        log.info("{} found in meta cache".format(domain_key))
        domain_exist = True
    else:
        domain_exist = await isS3Obj(app, domain_key)
    if not domain_exist:
        # if the domain is not found, return a 404
        msg = "Domain {} not found".format(domain)
        log.info(msg)
        raise HttpProcessingError(code=404, message=msg) 

    # delete S3 obj 
    await deleteS3Obj(app, domain_key)
    log.info("checking if s3obj is deleted")
    domain_exist = await isS3Obj(app, domain_key)
    if domain_exist:
        log.error("obj still there")
        raise HttpProcessingError(code=500, message="unexpected error")

  
    if domain_key in meta_cache:
        log.info("removing {} from meta_cache".format(domain_key))  
        del meta_cache[domain_key]  # delete from the meta cache
    deleted_ids.add(domain_key)

    json_response = { "domain": domain }

    resp = await jsonResponse(request, json_response, status=200)
    log.response(request, resp=resp)
    return resp

async def PUT_ACL(request):
    """ Handler creating/update an ACL"""
    log.request(request)
    app = request.app
    acl_username = request.match_info.get('username')

    if not request.has_body:
        msg = "Expected Body to be in request"
        log.warn(msg)
        raise HttpProcessingError(code=500, message=msg) 

    body_json = await request.json()

    if "domain" not in body_json:
        msg = "Missing domain"
        log.warn(msg)
        raise HttpBadRequest(msg)

    domain = body_json["domain"]
    log.info("domain: {}".format(domain))

    domain_key = getS3KeyForDomain(domain)
    log.info("domain key: {}".format(domain_key))

    if getObjPartition(domain_key, app['node_count']) != app['node_number']:
        # The request shouldn't have come to this node'
        raise HttpProcessingError(code=500, message="wrong node for 'key':{}".format(domain_key))
    
    meta_cache = app['meta_cache'] 
    domain_json = None
    if domain_key in meta_cache:
        log.info("{} found in meta cache".format(domain_key))
        domain_json = meta_cache[domain_key]
    else:
        log.info("getS3JSONObj({})".format(domain_key))
        # read S3 object as JSON
        domain_json = await getS3JSONObj(app, domain_key)
         
        meta_cache[domain_key] = domain_json  # add to cache

    if "acls" not in domain_json:
        log.error( "unexpected domain data for domain: {}".format(domain))
        raise HttpProcessingError(code=500, message="Unexpected Error")

    acl_keys = getAclKeys()
    acls = domain_json["acls"]
    acl = {}
    if acl_username in acls:
        acl = acls[acl_username]
    else:
        # initialize acl with no perms
        for k in acl_keys:
            acl[k] = False

    # replace any permissions given in the body
    for k in body_json.keys():
        acl[k] = body_json[k]

    # replace/insert the updated/new acl
    acls[acl_username] = acl
    
    # write back to S3
    now = time.time()
    dirty_ids = app["dirty_ids"]
    dirty_ids[domain_key] = now
    domain_json["lastModified"] = now
    
    resp_json = { } 
     
    resp = await jsonResponse(request, resp_json, status=201)
    log.response(request, resp=resp)
    return resp


   