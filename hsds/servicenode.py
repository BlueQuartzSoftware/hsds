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
 
from aiohttp.web import  run_app
from aiohttp import ClientSession, TCPConnector 
 
import config
from basenode import healthCheck,  baseInit
import hsds_logger as log
from domain_sn import GET_Domain, PUT_Domain
from group_sn import GET_Group, POST_Group 
 

async def init(loop):
    """Intitialize application and return app object"""
    app = baseInit(loop, 'sn')

    #
    # call app.router.add_get() here to add node-specific routes
    #
    app.router.add_route('GET', '/', GET_Domain)
    app.router.add_route('PUT', '/', PUT_Domain)
    app.router.add_route('GET', '/groups/{id}', GET_Group)
    app.router.add_route('POST', '/groups', POST_Group)
      
    return app

#
# Main
#

if __name__ == '__main__':
    log.info("Servicenode initializing")
    loop = asyncio.get_event_loop()

    # create a client Session here so that all client requests 
    #   will share the same connection pool
    max_tcp_connections = int(config.get("max_tcp_connections"))
    client = ClientSession(loop=loop, connector=TCPConnector(limit=max_tcp_connections))

    #create the app object
    app = loop.run_until_complete(init(loop))
    app['client'] = client
    app['domain_cache'] = {}
    if config.get("allow_noauth"):
        app['allow_noauth'] = True
    else:
        app['allow_noauth'] = False


    # run background task
    asyncio.ensure_future(healthCheck(app), loop=loop)
   
    # run the app
    run_app(app, port=config.get("sn_port"))
