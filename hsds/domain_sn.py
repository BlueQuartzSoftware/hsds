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

from aiohttp import  HttpProcessingError 
from aiohttp.errors import HttpBadRequest

from util.httpUtil import  http_post, http_put, jsonResponse
from util.idUtil import  getDataNodeUrl, createObjId
from util.authUtil import getUserPasswordFromRequest, aclCheck, validateUserPassword
from util.domainUtil import getParentDomain, getDomainFromRequest, isValidDomain
from servicenode_lib import getDomainJson
import hsds_logger as log


async def GET_Domain(request):
    """HTTP method to return JSON for given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
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
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "read", username)

    # return just the keys as per the REST API
    rsp_json = { }
    rsp_json["root"] = domain_json["root"]
    rsp_json["owner"] = domain_json["owner"]

    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def PUT_Domain(request):
    """HTTP method to create a new domain"""
    log.request(request)
    app = request.app
    # yet exist
    username, pswd = getUserPasswordFromRequest(request) # throws exception if user/password is not valid
    validateUserPassword(username, pswd)
    log.info("PUT domain request from: {}".format(username))
    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = "Invalid host value: {}".format(domain)
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    parent_domain = getParentDomain(domain)
    if parent_domain is None:
        msg = "creation of top-level domains is not supported"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    log.info("parent_domain: {}".format(parent_domain))
    parent_json = None
    try:
        log.info("get parent domain {}".format(parent_domain))
        parent_json = await getDomainJson(app, parent_domain)
    except HttpProcessingError as hpe:
        msg = "Parent domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)

    aclCheck(parent_json, "create", username)  # throws exception if not allowed
    
    # create a root group for the new domain
    # TBD - fire off create group and create domain dn requests at the same time
    root_id = createObjId("group") 
    log.info("new root group id: {}".format(root_id))
    group_json = {"id": root_id, "root": root_id, "domain": domain }
    log.info("create group for domain, body: " + json.dumps(group_json))
    req = getDataNodeUrl(app, root_id) + "/groups"
    try:
        group_json = await http_post(app, req, group_json)
    except HttpProcessingError as ce:
        msg="Error creating root group for domain -- " + str(ce)
        log.warn(msg)
        raise ce
 
    domain_json = { }

    # construct dn request to create new domain
    req = getDataNodeUrl(app, domain)
    req += "/domains/" + domain 
    body = { "owner": username }
    body["acls"] = parent_json["acls"]  # copy parent acls to new domain
    body["root"] = root_id

    try:
        domain_json = await http_put(app, req, body)
    except HttpProcessingError as ce:
        msg="Error creating domain state -- " + str(ce)
        log.warn(msg)
        raise ce

    # domain creation successful     
    resp = await jsonResponse(request, domain_json, status=201)
    log.response(request, resp=resp)
    return resp

 