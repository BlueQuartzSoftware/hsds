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
import os

cfg = {
    'root_dir': '', # set to directory when using posix driver
    'hsds_unit_test_bucket': '',   # bucket to read and write test data, e.g. 'hsds.util.test'
    'azure_connection_string': '',
    'aws_s3_gateway' : '',
    'log_level': "DEBUG"
}

def get(x):
    # see if there are an environment variable override
    if x.upper() in os.environ:
        return os.environ[x.upper()]
    # no command line override, just return the cfg value
    return cfg[x]
