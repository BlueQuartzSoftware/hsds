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

from aiohttp.web import run_app
from aiohttp import ClientSession, TCPConnector 
import config
from basenode import healthCheck, baseInit
import hsds_logger as log
from domain_dn import GET_Domain, PUT_Domain, DELETE_Domain
from group_dn import GET_Group, POST_Group, DELETE_Group
from link_dn import GET_Links, GET_Link, PUT_Link, DELETE_Link
from attr_dn import GET_Attributes, GET_Attribute, PUT_Attribute, DELETE_Attribute
from datanode_lib import s3sync 

               

async def init(loop):
    """Intitialize application and return app object"""
    app = baseInit(loop, 'dn')

    #
    # call app.router.add_get() here to add node-specific routes
    #
    app.router.add_route('GET', '/domains/{key}', GET_Domain)
    app.router.add_route('PUT', '/domains/{key}', PUT_Domain)
    app.router.add_route('DELETE', '/domains/{key}', DELETE_Domain)
    app.router.add_route('GET', '/groups/{id}', GET_Group)
    app.router.add_route('DELETE', '/groups/{id}', DELETE_Group)
    app.router.add_route('POST', '/groups', POST_Group)
    app.router.add_route('GET', '/groups/{id}/links', GET_Links)
    app.router.add_route('GET', '/groups/{id}/links/{title}', GET_Link)
    app.router.add_route('DELETE', '/groups/{id}/links/{title}', DELETE_Link)
    app.router.add_route('PUT', '/groups/{id}/links/{title}', PUT_Link)
    app.router.add_route('GET', '/groups/{id}/attributes', GET_Attributes)
    app.router.add_route('GET', '/groups/{id}/attributes/{name}', GET_Attribute)
    app.router.add_route('DELETE', '/groups/{id}/attributes/{name}', DELETE_Attribute)
    app.router.add_route('PUT', '/groups/{id}/attributes/{name}', PUT_Attribute)
      
    return app

#
# Main
#

if __name__ == '__main__':
    log.info("datanode start")
    loop = asyncio.get_event_loop()

    # create a client Session here so that all client requests 
    #   will share the same connection pool
    max_tcp_connections = int(config.get("max_tcp_connections"))
    client = ClientSession(loop=loop, connector=TCPConnector(limit=max_tcp_connections))

    #create the app object
    app = loop.run_until_complete(init(loop))
    app['client'] = client
    app['meta_cache'] = {}
    app['data_cache'] = {}
    app['deleted_ids'] = set()
    app['dirty_ids'] = {}

    # run background tasks
    asyncio.ensure_future(healthCheck(app), loop=loop)

    # run data sync tasks
    asyncio.ensure_future(s3sync(app), loop=loop)


   
    # run the app
    run_app(app, port=config.get("dn_port"))
