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
# service node of hsds cluster
# 
 
import json
from aiohttp.errors import HttpBadRequest
 
from util.httpUtil import  http_get_json, http_post, http_delete, jsonResponse
from util.idUtil import   isValidUuid, getDataNodeUrl, createObjId
from util.authUtil import getUserPasswordFromRequest, aclCheck, validateUserPassword
from util.domainUtil import  getDomainFromRequest, isValidDomain
from servicenode_lib import getDomainJson
import hsds_logger as log



async def GET_Group(request):
    """HTTP method to return JSON for group"""
    log.request(request)
    app = request.app 

    group_id = request.match_info.get('id')
    if not group_id:
        msg = "Missing group id"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(group_id, "Group"):
        msg = "Invalid group id: {}".format(group_id)
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
    
    domain_json = await getDomainJson(app, domain)
    aclCheck(domain_json, "read", username)  # throws exception if not allowed

    if "root" not in domain_json:
        msg = "Domain has no root group"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    req = getDataNodeUrl(app, group_id)
    req += "/groups/" + group_id

    group_json = await http_get_json(app, req)   

    if group_json["root"] != domain_json["root"]:
        msg = "Group id is not a member of the given domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

 
    resp = await jsonResponse(request, group_json)
    log.response(request, resp=resp)
    return resp

async def POST_Group(request):
    """HTTP method to return JSON for group"""
    log.request(request)
    app = request.app

    username, pswd = getUserPasswordFromRequest(request)
    # write actions need auth
    validateUserPassword(username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    domain_json = await getDomainJson(app, domain)

    aclCheck(domain_json, "create", username)  # throws exception if not allowed

    if "root" not in domain_json:
        msg = "Expected root key for domain: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    root_id = domain_json["root"]
    group_id = createObjId("group") 
    log.info("new  group id: {}".format(group_id))
    group_json = {"id": group_id, "root": root_id, "domain": domain }
    log.info("create group, body: " + json.dumps(group_json))
    req = getDataNodeUrl(app, group_id) + "/groups"
    
    group_json = await http_post(app, req, group_json)
    
    # domain creation successful     
    resp = await jsonResponse(request, group_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Group(request):
    """HTTP method to delete a group resource"""
    log.request(request)
    app = request.app 

    group_id = request.match_info.get('id')
    if not group_id:
        msg = "Missing group id"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    if not isValidUuid(group_id, "Group"):
        msg = "Invalid group id: {}".format(group_id)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    username, pswd = getUserPasswordFromRequest(request)
    validateUserPassword(username, pswd)
    
    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    domain_json = await getDomainJson(app, domain)
    aclCheck(domain_json, "delete", username)  # throws exception if not allowed

    if "root" not in domain_json:
        msg = "Domain has no root group"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    req = getDataNodeUrl(app, group_id)
    req += "/groups/" + group_id
    
    rsp_json = await http_delete(app, req)
 
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

 