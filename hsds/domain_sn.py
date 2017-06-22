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
import asyncio 
import json
from aiohttp.errors import HttpBadRequest, HttpProcessingError

from util.httpUtil import  http_post, http_put, http_get_json, http_delete, jsonResponse, getHref
from util.idUtil import  getDataNodeUrl, createObjId, getS3Key, getCollectionForId
from util.s3Util import getS3Keys, isS3Obj, getS3Bytes, getS3ObjStats
from util.authUtil import getUserPasswordFromRequest, aclCheck
from util.authUtil import validateUserPassword, getAclKeys
from util.domainUtil import getParentDomain, getDomainFromRequest, getS3PrefixForDomain, validateDomain, isIPAddress, isValidDomainPath
from servicenode_lib import getDomainJson
import hsds_logger as log

async def get_domain_json(app, domain):
    req = getDataNodeUrl(app, domain)
    req += "/domains" 
    params = {"domain": domain}
    log.info("sending dn req: {}".format(req))
    domain_json = await http_get_json(app, req, params=params)
    return domain_json

async def domain_query(app, domain, rsp_dict):
    try :
        domain_json = await get_domain_json(app, domain)
        rsp_dict[domain] = domain_json
    except HttpProcessingError as hpe:
        rsp_dict[domain] = { "status_code": hpe.code}

async def get_collection(app, domain, collection, marker=None, limit=None):
    """ Return the object ids from the collections.txt obj for given collection.
    """    
    col_s3key = domain[1:] + "/." + collection + ".txt"  
    log.info("get collection list: {}".format(col_s3key))
    col_found = await isS3Obj(app, col_s3key)
    if not col_found:
        return []
    rows = []
    
    data = await getS3Bytes(app, col_s3key)
    data = data.decode('utf8')
    lines = data.split('\n')
    for line in lines:
        # format is: 
        # <objid> <size>\n
        if not line:
            continue
        fields = line.split(' ')
        if len(fields) < 4:
            log.warn("Unexpected contents line: {}".format(line))
            continue
        objid = fields[0]
        if not objid:
            continue
        try:
            if getCollectionForId(objid) != collection:
                log.warn("unexpected objectid: {}".format(objid))
                continue
        except ValueError as ve:
            log.warn("unexpected exception for get collections: {}".format(str(ve)))
            continue

        if marker:
            if marker == objid:
                # got to the marker, clear it so we will start 
                # return ids on the next iteration
                marker = None
        else:
            # return objid, etag, lastModified, and size
            row = []
            row.append(objid)
            row.append(fields[1])  # etag
            try:
                row.append(float(fields[2]))
            except ValueError:
                log.warn("Unexpected contents line (3rd element should be float): {}".format(line))
                continue
            try:
                row.append(int(fields[3]))
            except ValueError:
                log.warn("Unexpected contents line (4th element should be int): {}".format(line))
                continue
            # dataset contents will have extra fields for numchunks and chunk size
            if len(fields) > 4:
                try:
                    row.append(int(fields[4]))
                except ValueError:
                    log.warn("Unexpected contents line (5th element should be int): {}".format(line))
                    continue
            if len(fields) > 5:
                try:
                    row.append(int(fields[5]))
                except ValueError:
                    log.warn("Unexpected contents line (6th element should be int): {}".format(line))
                    continue

            rows.append(row)
            if limit is not None and len(rows) == limit:
                log.info("got to limit, breaking")
                break
    return rows

async def getDomainInfo(app, domain):
    """ Get extra information about the given domain """
    # Gather additional info on the domain
    results = {}
    allocated_bytes = 0
    num_chunks = 0
    group_collection = await get_collection(app, domain, "groups")
    results["num_groups"] = len(group_collection) 
    for row in group_collection:
        allocated_bytes += row[3]

    datatype_collection = await get_collection(app, domain, "datatypes")
    results["num_datatypes"] = len(datatype_collection) 
    for row in datatype_collection:
        allocated_bytes += row[3]
    
    dataset_collection = await get_collection(app, domain, "datasets")
    results["num_datasets"] = len(dataset_collection) 
    for row in dataset_collection:
        allocated_bytes += row[3]
        if len(row) > 5:
            num_chunks += row[4]
            allocated_bytes += row[5]
    # get size of the domain json object itself
    s3_key = getS3Key(domain)
    stats = await getS3ObjStats(app, s3_key)
                  
    allocated_bytes += stats["Size"]
    results["allocated_bytes"] = allocated_bytes
    results["num_chunks"] = num_chunks
    return results
        
async def get_domains(request):
    """ This method is called by GET_Domains and GET_Domain when no domain is passed in.
    """
    app = request.app
    loop = app["loop"]
    # if there is no domain passed in, get a list of top level domains
    log.info("get_domains")
    domain = None
    if "domain" in request.GET or "host" in request.GET or not isIPAddress(request.host):
        try:
            log.debug("getDomainFromRequest")
            domain = getDomainFromRequest(request, domain_path=True, validate=False)
        except ValueError:
            msg = "Invalid domain"
            log.warn(msg)
            raise HttpBadRequest(message=msg)

        log.info("got domain: [{}]".format(domain))
    else:
        log.info("get top level domains")

    log.debug("getDomainFromRequest returned: [{}]".format(domain))
    if domain and not isValidDomainPath(domain):
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    if domain == '/':
        domain = None  # to simplify logic below
    if domain is not None:
        domain_prefix = getS3PrefixForDomain(domain)
        log.debug("using domain prefix: {}".format(domain_prefix))
     
    limit = None
    if "Limit" in request.GET:
        try:
            limit = int(request.GET["Limit"])
            log.debug("GET_Domains - using Limit: {}".format(limit))
        except ValueError:
            msg = "Bad Request: Expected int type for limit"
            log.error(msg)  # should be validated by SN
            raise HttpBadRequest(message=msg)
    marker_key = None
    if "Marker" in request.GET:
        marker = request.GET["Marker"]
        log.debug("got Marker request param: {}".format(marker))
        try:
            # marker should be a valid domain
            validateDomain(marker)
        except ValueError:
            msg = "Invalid marker value: {}".format(marker)
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        marker_key = getS3Key(marker)
        log.debug("GET_Domains - using Marker key: {}".format(marker_key))
    verbose = False
    if "verbose" in request.GET and request.GET["verbose"]:
        verbose = True

    s3_keys = []
    if domain is None:
        # return list of toplevel domains
        topleveldomains_key = "topleveldomains.txt"
        col_found = await isS3Obj(app, topleveldomains_key)
        if not col_found:
            log.warn("{} key not found".format(topleveldomains_key))
        else:
            data = await getS3Bytes(app, topleveldomains_key)
            data = data.decode('utf8')
            lines = data.split('\n')
            log.info("{} lines: {}".format(topleveldomains_key, lines))
            for line in lines:
                if not line:
                    continue
                if line[0] != '/':
                    log.warn("unexpected line in {}: {}".format(topleveldomains_key, line))
                    continue
                s3_keys.append(line[1:])  # strip of leading slash
    else:
        s3_keys = await getS3Keys(app, prefix=domain_prefix, deliminator='/')
        log.debug("got {} keys".format(len(s3_keys)))
    # filter out anything without a '/' in the key
    # note: sometimes a ".domain.json" key shows up, not sure why
    keys = []
    for key in s3_keys:
        if domain is not None and key.find('/') == -1:
            log.debug('skipping key: {}'.format(key))
            continue
        keys.append(key)

    log.debug("s3keys: {}".format(keys))
    if marker_key:
        # trim everything up to and including marker
        log.debug("using marker key: {}".format(marker_key))
        index = 0
        for key in keys:
            index += 1
            log.debug("compare {} to {}".format(key, marker_key))
            if key == marker_key:
                break
            # also check if this matches key with ".domain.json" appended
            if key + ".domain.json" == marker_key:
                break

        if index > 0:
            keys = keys[index:]

    if limit and len(keys) > limit:
        keys = keys[:limit]  
        log.debug("restricting number of keys returned to limit value")

    log.debug("s3keys trim to marker and limit: {}".format(keys))
    
    if len(keys) > 0:
        dn_rsp = {} # dictionary keyed by chunk_id
        tasks = []
        log.debug("async query with {} domain keys".format(len(keys)))
        for key in keys:
            sub_domain = '/' + key
            if sub_domain[-1] == '/':
                sub_domain = sub_domain[:-1]  # specific sub-domains don't have trailing slash
            log.debug("query for subdomain: {}".format(sub_domain))
            task = asyncio.ensure_future(domain_query(app, sub_domain, dn_rsp))
            tasks.append(task)
        await asyncio.gather(*tasks, loop=loop)
        log.debug("async query complete")

    domains = []
    for key in keys:
        sub_domain = '/' + key  
        if sub_domain[-1] == '/':
            sub_domain = sub_domain[:-1]  # specific sub-domains don't have trailing slash
        log.debug("sub_domain: {}".format(sub_domain))
        if sub_domain not in dn_rsp:
            log.warn("expected to find sub-domain: {} in dn_rsp".format(sub_domain))
            continue
        sub_domain_json = dn_rsp[sub_domain]
        if "status_code" in sub_domain_json:
            # some error happened for this request
            status_code = sub_domain_json["status_code"]
            if status_code == 401:
                log.warn("No permission for reading sub_domain: {}".format(sub_domain))
            elif status_code == 404:
                log.warn("Not found error for sub_domain: {}".format(sub_domain))
            elif status_code == 410:
                log.info("Key removed error for sub_domain: {}".format(sub_domain))
            else:
                msg = "Unexpected error: {}".format(status_code)
                log.warn(msg)
                raise HttpProcessingError(code=status_code, message=msg)
            continue  # go on to next key
        domain_rsp = {"name": sub_domain}
        if "owner" in sub_domain_json:
            domain_rsp["owner"] = sub_domain_json["owner"]
        if "created" in sub_domain_json:
            domain_rsp["created"] = sub_domain_json["created"]
        if "lastModified" in sub_domain_json:
            domain_rsp["lastModified"] = sub_domain_json["lastModified"]
        if "root" in sub_domain_json:
            domain_rsp["class"] = "domain"
        else:
            domain_rsp["class"] = "folder"
        if verbose:
            # get info from collection files
            results = await getDomainInfo(app, sub_domain)
            for k in ("num_groups", "num_datatypes", "num_datasets", "allocated_bytes", "num_chunks"):
                if k in results:
                    domain_rsp[k] = results[k]

        domains.append(domain_rsp)
    rsp_json = {}
    rsp_json["domains"] = domains
    rsp_json["href"] = [] # TBD

    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def GET_Domains(request):
    """HTTP method to return JSON for child domains of given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)

    return await get_domains(request)

 
async def GET_Domain(request):
    """HTTP method to return JSON for given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)

    if "domain" not in request.GET and "host" not in request.GET and isIPAddress(request.host):
        # no domain passed in, return top-level domains for this request
        return await get_domains(request)

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    log.info("got domain: {}".format(domain))
   
    domain_json = await get_domain_json(app, domain)

    if domain_json is None:
        log.warn("domain: {} not found".format(domain))
        raise HttpProcessingError(code=404, message="Not Found")
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    log.debug("got domain_json: {}".format(domain_json))
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "read", username)  # throws exception if not authorized

    # return just the keys as per the REST API
    rsp_json = { }
    if "root" in domain_json:
        rsp_json["root"] = domain_json["root"]
        rsp_json["class"] = "domain"
    else:
        rsp_json["class"] = "folder"
    if "owner" in domain_json:
        rsp_json["owner"] = domain_json["owner"]
    if "created" in domain_json:
        rsp_json["created"] = domain_json["created"]
    if "lastModified" in domain_json:
        rsp_json["lastModified"] = domain_json["lastModified"]
     
    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/')})
    if "root" in domain_json:
        root_uuid = domain_json["root"]
        hrefs.append({'rel': 'database', 'href': getHref(request, '/datasets')})
        hrefs.append({'rel': 'groupbase', 'href': getHref(request, '/groups')})
        hrefs.append({'rel': 'typebase', 'href': getHref(request, '/datatypes')})
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + root_uuid)})
    
    hrefs.append({'rel': 'acls', 'href': getHref(request, '/acls')})
    parent_domain = getParentDomain(domain)
    log.debug("href parent domain: {}".format(parent_domain))
    if parent_domain:
        hrefs.append({'rel': 'parent', 'href': getHref(request, '/', domain=parent_domain)})

    if "verbose" in request.GET and request.GET["verbose"]:
        results = await getDomainInfo(app, domain)
        for k in ("num_groups", "num_datatypes", "num_datasets", "allocated_bytes", "num_chunks"):
            if k in results:
                rsp_json[k] = results[k]
         
    rsp_json["hrefs"] = hrefs
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def PUT_Domain(request):
    """HTTP method to create a new domain"""
    log.request(request)
    app = request.app
    # yet exist
    username, pswd = getUserPasswordFromRequest(request) # throws exception if user/password is not valid
    validateUserPassword(app, username, pswd)
    
    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
 
    log.info("PUT domain: {}, username: {}".format(domain, username))

    parent_domain = getParentDomain(domain)

    if not parent_domain:
        msg = "creation of top-level domains is not supported"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    log.debug("parent_domain: {}".format(parent_domain))

    parent_json = None
    try:
        parent_json = await getDomainJson(app, parent_domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "Parent domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)

    body = None
    is_folder = False
    if request.has_body:
        body = await request.json()   
        log.debug("PUT domain with body: {}".format(body))
        if body and "folder" in body:
            if body["folder"]:
                is_folder = True

    aclCheck(parent_json, "create", username)  # throws exception if not allowed
    
    if not is_folder:
        # create a root group for the new domain
        root_id = createObjId("groups") 
        log.debug("new root group id: {}".format(root_id))
        group_json = {"id": root_id, "root": root_id, "domain": domain }
        log.debug("create group for domain, body: " + json.dumps(group_json))
    
        # create root group
        req = getDataNodeUrl(app, root_id) + "/groups"
        try:
            group_json = await http_post(app, req, data=group_json)
        except HttpProcessingError as ce:
            msg="Error creating root group for domain -- " + str(ce)
            log.error(msg)
            raise ce
    else:
        log.debug("no root group, creating folder")
 
    domain_json = { }

    # construct dn request to create new domain
    req = getDataNodeUrl(app, domain)
    req += "/domains" 
    body = { "owner": username, "domain": domain }
    body["acls"] = parent_json["acls"]  # copy parent acls to new domain

    if not is_folder:
        body["root"] = root_id
    try:
        domain_json = await http_put(app, req, data=body)
    except HttpProcessingError as ce:
        msg="Error creating domain state -- " + str(ce)
        log.warn(msg)
        raise ce

    # domain creation successful     
    resp = await jsonResponse(request, domain_json, status=201)
    log.response(request, resp=resp)
    return resp

async def DELETE_Domain(request):
    """HTTP method to delete a domain resource"""
    log.request(request)
    app = request.app 

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
 
    username, pswd = getUserPasswordFromRequest(request)
    validateUserPassword(app, username, pswd)

    parent_domain = getParentDomain(domain)

    # verify that this is not a top-level domain
    if not parent_domain:
        msg = "Top level domain can not be deleted"
        log.warn(msg)
        raise HttpProcessingError(code=403, message="Forbidden")

    # get the parent domain
    try:
        log.debug("get parent domain {}".format(parent_domain))
        parent_json = await getDomainJson(app, parent_domain)
    except HttpProcessingError as hpe:
        msg = "Attempt to delete domain with no parent domain"
        log.warn(msg)
        raise HttpProcessingError(code=403, message="Forbidden")
    log.debug("got parent json: {}".format(parent_json))
    
    try:
        domain_json = await getDomainJson(app, domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)

    aclCheck(domain_json, "delete", username)  # throws exception if not allowed

    req = getDataNodeUrl(app, domain)
    req += "/domains" 
    body = { "domain": domain }
    
    rsp_json = await http_delete(app, req, data=body)
 
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def GET_ACL(request):
    """HTTP method to return JSON for given domain/ACL"""
    log.request(request)
    app = request.app

    acl_username = request.match_info.get('username')
    if not acl_username:
        msg = "Missing username for ACL"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    # use reload to get authoritative domain json
    try:
        domain_json = await getDomainJson(app, domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)
     
    # validate that the requesting user has permission to read ACLs in this domain
    if acl_username in (username, "default"):
        # allow read access for a users on ACL, or default
        aclCheck(domain_json, "read", username)  # throws exception if not authorized
    else:
        aclCheck(domain_json, "readACL", username)  # throws exception if not authorized
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    acls = domain_json["acls"]

    log.debug("got domain_json: {}".format(domain_json))

    if acl_username not in acls:
        msg = "acl for username: [{}] not found".format(acl_username)
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)

    acl = acls[acl_username]
    acl_rsp = {}
    for k in acl.keys():
        acl_rsp[k] = acl[k]
    acl_rsp["userName"] = acl_username

    # return just the keys as per the REST API
    rsp_json = { }
    rsp_json["acl"] = acl_rsp
    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/acls')})
    if "root" in domain_json:
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + domain_json["root"])})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})
    hrefs.append({'rel': 'owner', 'href': getHref(request, '/')})
    rsp_json["hrefs"] = hrefs

    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def GET_ACLs(request):
    """HTTP method to return JSON for domain/ACLs"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    # use reload to get authoritative domain json
    try:
        domain_json = await getDomainJson(app, domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    acls = domain_json["acls"]

    log.debug("got domain_json: {}".format(domain_json))
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "readACL", username)  # throws exception if not authorized

    acl_list = []
    acl_usernames = list(acls.keys())
    acl_usernames.sort()
    for acl_username in acl_usernames:
        entry = {"userName": acl_username}
        acl = acls[acl_username]

        for k in acl.keys():
            entry[k] = acl[k]
        acl_list.append(entry)
    # return just the keys as per the REST API
    rsp_json = { }
    rsp_json["acls"] = acl_list

    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/acls')})
    if "root" in domain_json:
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + domain_json["root"])})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})
    hrefs.append({'rel': 'owner', 'href': getHref(request, '/')})
    rsp_json["hrefs"] = hrefs

    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp    

async def PUT_ACL(request):
    """HTTP method to add a new ACL for a domain"""
    log.request(request)
    app = request.app

    acl_username = request.match_info.get('username')
    if not acl_username:
        msg = "Missing username for ACL"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    (username, pswd) = getUserPasswordFromRequest(request)
    validateUserPassword(app, username, pswd)

    if not request.has_body:
        msg = "PUT ACL with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    body = await request.json()   
    acl_keys = getAclKeys()

    for k in body.keys():
        if k not in acl_keys:
            msg = "Unexpected key in request body: {}".format(k)
            log.warn(msg)
            raise HttpBadRequest(message=msg)
        if body[k] not in (True, False):
            msg = "Unexpected value for key in request body: {}".format(k)
            log.warn(k)
            raise HttpBadRequest(message=msg)

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

     
    # don't use app["domain_cache"]  if a direct domain request is made 
    # as opposed to an implicit request as with other operations, query
    # the domain from the authoritative source (the dn node)
    req = getDataNodeUrl(app, domain)
    req += "/acls/" + acl_username
    log.info("sending dn req: {}".format(req))    
    body["domain"] = domain

    put_rsp = await http_put(app, req, data=body)
    log.info("PUT ACL resp: " + str(put_rsp))
    
    # ACL update successful     
    resp = await jsonResponse(request, put_rsp, status=201)
    log.response(request, resp=resp)
    return resp


async def GET_Datasets(request):
    """HTTP method to return dataset collection for given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)

    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    # verify the domain 
    try:
        domain_json = await getDomainJson(app, domain)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    log.debug("got domain_json: {}".format(domain_json))
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "read", username)  # throws exception if not authorized

    limit = None
    if "Limit" in request.GET:
        try:
            limit = int(request.GET["Limit"])
        except ValueError:
            msg = "Bad Request: Expected int type for limit"
            log.warn(msg)
            raise HttpBadRequest(message=msg)
    marker = None
    if "Marker" in request.GET:
        marker = request.GET["Marker"]


    # get the dataset collection list
    datasets = await get_collection(app, domain, "datasets", marker=marker, limit=limit)
    obj_ids = []
    for row in datasets:
        # row consist of objectid, etag, lastmodified, and size
        # return just the objid
        obj_ids.append(row[0]) 
     
    # create hrefs 
    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/datasets')})
    if "root" in domain_json:
        root_uuid = domain_json["root"]
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + root_uuid)})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})

    # return obj ids and hrefs
    rsp_json = { }
    rsp_json["datasets"] = obj_ids
    rsp_json["hrefs"] = hrefs
     
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def GET_Groups(request):
    """HTTP method to return groups collection for given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)
    
    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    # use reload to get authoritative domain json
    try:
        domain_json = await getDomainJson(app, domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    log.debug("got domain_json: {}".format(domain_json))
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "read", username)  # throws exception if not authorized

    # get the groups collection list
    limit = None
    if "Limit" in request.GET:
        try:
            limit = int(request.GET["Limit"])
        except ValueError:
            msg = "Bad Request: Expected int type for limit"
            log.warn(msg)
            raise HttpBadRequest(message=msg)
    marker = None
    if "Marker" in request.GET:
        marker = request.GET["Marker"]

    # get the dataset collection list
    groups = await get_collection(app, domain, "groups", marker=marker, limit=limit)
    obj_ids = []
    for row in groups:
        # row consist of objectid, etag, lastmodified, and size
        # return just the objid
        obj_ids.append(row[0]) 
     
    # create hrefs 
    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/groups')})
    if "root" in domain_json:
        root_uuid = domain_json["root"]
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + root_uuid)})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})

    # return obj ids and hrefs
    rsp_json = { }
    rsp_json["groups"] = obj_ids
    rsp_json["hrefs"] = hrefs
     
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp

async def GET_Datatypes(request):
    """HTTP method to return datatype collection for given domain"""
    log.request(request)
    app = request.app

    (username, pswd) = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        validateUserPassword(app, username, pswd)
    
    try:
        domain = getDomainFromRequest(request)
    except ValueError:
        msg = "Invalid domain"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    
    # use reload to get authoritative domain json
    try:
        domain_json = await getDomainJson(app, domain, reload=True)
    except HttpProcessingError as hpe:
        msg = "domain not found"
        log.warn(msg)
        raise HttpProcessingError(code=404, message=msg)
     
    if 'owner' not in domain_json:
        log.warn("No owner key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    if 'acls' not in domain_json:
        log.warn("No acls key found in domain")
        raise HttpProcessingError(code=500, message="Unexpected error")

    log.debug("got domain_json: {}".format(domain_json))
    # validate that the requesting user has permission to read this domain
    aclCheck(domain_json, "read", username)  # throws exception if not authorized

    limit = None
    if "Limit" in request.GET:
        try:
            limit = int(request.GET["Limit"])
        except ValueError:
            msg = "Bad Request: Expected int type for limit"
            log.warn(msg)
            raise HttpBadRequest(message=msg)
    marker = None
    if "Marker" in request.GET:
        marker = request.GET["Marker"]

    # get the datatype collection list
    datatypes = await get_collection(app, domain, "datatypes", marker=marker, limit=limit)

    obj_ids = []
    for row in datatypes:
        # row consist of objectid, etag, lastmodified, and size
        # return just the objid
        obj_ids.append(row[0]) 
     
    # create hrefs 
    hrefs = []
    hrefs.append({'rel': 'self', 'href': getHref(request, '/datatypes')})
    if "root" in domain_json:
        root_uuid = domain_json["root"]
        hrefs.append({'rel': 'root', 'href': getHref(request, '/groups/' + root_uuid)})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})

    # return obj ids and hrefs
    rsp_json = { }
    rsp_json["datatypes"] = obj_ids
    rsp_json["hrefs"] = hrefs
     
    resp = await jsonResponse(request, rsp_json)
    log.response(request, resp=resp)
    return resp
    


 