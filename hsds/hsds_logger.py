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
# Simple looger for hsds
#
import asyncio
import config
from aiohttp.web_exceptions import HTTPServiceUnavailable
from util.domainUtil import getDomainFromRequest
app = None # global app handle

def debug(msg):
	if config.get("log_level") == "DEBUG":
		print("DEBUG> " + msg)
	if app:
		counter = app["log_count"]
		counter["DEBUG"] += 1

def info(msg):
	if config.get("log_level") not in  ("ERROR", "WARNING", "WARN"):  
		print("INFO> " + msg)
	if app:
		counter = app["log_count"]
		counter["INFO"] += 1

def warn(msg):
	if config.get("log_level") != "ERROR":
		print("WARN> " + msg)
	if app:
		counter = app["log_count"]
		counter["WARN"] += 1

def warning(msg):
	if config.get("log_level") != "ERROR":
		print("WARN> " + msg)
	if app:
		counter = app["log_count"]
		counter["WARN"] += 1

def error(msg):
	print("ERROR> " + msg)
	if app:
		counter = app["log_count"]
		counter["ERROR"] += 1

def request(req):
	domain = getDomainFromRequest(req, validate=False)
	if domain is None:
		print("REQ> {}: {}".format(req.method, req.path))
	else:
		print("REQ> {}: {} [{}]".format(req.method, req.path, domain))
	if app:
		counter = app["req_count"]
		if req.method in ("GET", "POST", "PUT", "DELETE"):
			counter[req.method] += 1
		num_tasks = len(asyncio.Task.all_tasks())
		active_tasks = len([task for task in asyncio.Task.all_tasks() if not task.done()])
		counter["num_tasks"] = num_tasks
		if config.get("log_level") == "DEBUG":
			print(f"DEBUG> num tasks: {num_tasks} active tasks: {active_tasks}")

		max_task_count = config.get("max_task_count")
		if app["node_type"] == "sn" and max_task_count and active_tasks > max_task_count:
			print(f"WARN: more than {max_task_count} tasks, returning 503")
			raise HTTPServiceUnavailable()


def response(req, resp=None, code=None, message=None):
	level = "INFO"
	if code is None:
		# rsp needs to be set otherwise
		code = resp.status
	if message is None:
		message=resp.reason
	if code > 399:
		if  code < 500:
			level = "WARN"
		else:
			level = "ERROR"
	
	log_level = config.get("log_level")
	if log_level in ("DEBUG", "INFO") or (log_level == "WARN" and level != "INFO") or (log_level == "ERROR" and level == "ERROR"):
		print("{} RSP> <{}> ({}): {}".format(level, code, message, req.path))
