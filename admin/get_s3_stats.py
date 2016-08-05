#!/usr/bin/env python

import sys, os, re
import urllib2, urllib
import datetime
import Queue
import threading
import logging
import json
import numpy

GDDP_INVENTORY = 'https://s3-us-west-2.amazonaws.com/nasanex/NEX-GDDP/nex-gddp-s3-files.json'
NTRHS = 4
JSN = None

#---------------------------------------------------------------------------------
def get_gddp_json(invlink):
   logging.info("getting inventory from %s" % (invlink))
   jdata = json.loads( urllib2.urlopen(invlink).read() )
   return jdata

#---------------------------------------------------------------------------------
def select_gddp_model(jdata, model):
   return  [ k for k in jdata.keys() if jdata[k]['model'] == model ]

#---------------------------------------------------------------------------------
def select_gddp_experiment(jdata, expr):
   return  [ k for k in jdata.keys() if jdata[k]['experiment_id'] == expr ]

#---------------------------------------------------------------------------------
def select_gddp_var(jdata, vr):
   return  [ k for k in jdata.keys() if jdata[k]['variable'] == vr ]

#---------------------------------------------------------------------------------
def get_gddp_sets(jdata):
   models = set( [jdata[k]['model'] for k in jdata.keys()] )
   exprs = set( [jdata[k]['experiment_id'] for k in jdata.keys()] )
   vrs = set( [jdata[k]['variable'] for k in jdata.keys()] )
   return models, exprs, vrs 

#---------------------------------------------------------------------------------
def get_remote_size(rfname):
   try:
      rfo = urllib.urlopen(rfname)
      cl = rfo.info().getheaders("Content-Length")[0]
      return float(cl)
   except Exception, e:
      logging.warn("WARN get_remote_size on %s failed : %s" % (rfname, str(e)))
      return None
#get_remote_size

#---------------------------------------------------------------------------------
def queue_list(invlink):
   queue = Queue.Queue()
   jsn = get_gddp_json(invlink)
   for k in jsn.keys():
      queue.put( k )
   return queue, jsn
#queue_list

#---------------------------------------------------------------------------------
def get_sizes():
   global JSN 
   thrd = threading.current_thread()
   logging.info('starting thread '+str(thrd.ident)+' ...')
   try:
      while True:
         if queue.empty() == True: 
            break
         itm = queue.get()
         logging.info(str(thrd.ident)+' :' +str(itm))
         val = get_remote_size(itm)
         if val != None: JSN[itm]['objsize'] = val
   except Queue.Empty, e: 
      pass
   logging.info('thread '+str(thrd.ident)+' done...') 
#get_sizes

#---------------------------------------------------------------------------------
def set_stat(jsn, items, selectfunc, hiswdth=64, lab='histogram'):
   d = {}
   lables, means, stds, sums = [], [], [], []
   for i in items:
      fls = selectfunc(jsn, i)
      d[i] = [ jsn[f]['objsize'] for f in fls ]
      np = numpy.array(d[i], dtype='f8')
      lables.append( i )
      means.append( np.mean() )
      stds.append( np.std() )
      sums.append( np.sum() )

   # print quasi historgram
   gb = 2.0**30.0
   sumsnp = numpy.array(sums, dtype='f8')
   sumsmax, sumsmin = sumsnp.max(), sumsnp.min() 
   sumall = sumsnp.sum() 
   fstr = "%15s |%s      [%0.1fGB, %0.1f std]" 
   print "\n\n%s, min=%.1f, max=%.1f total=%.0fGB\n%s" % (lab, sumsmin/gb, sumsmax/gb, sumall/gb, '-'*(hiswdth+15))
   for i, s in enumerate(sums): 
      nx = sums[i] / (sumsmax*1.2)
      hst = ''
      for x in range(0, int(round(hiswdth * nx))): hst += '*'
      for y in range(x, hiswdth): hst += ' '
      print fstr % (lables[i], hst, sums[i]/gb, stds[i]/gb)
#set_stat

#---------------------------------------------------------------------------------
def summarize_size(jsn):
   mstats, vrstats, exstats = [None]*3
   # get info break down
   models, exprs, vrs = get_gddp_sets(jsn)
   mstats = set_stat(jsn, models, select_gddp_model, lab='models')
   exstats = set_stat(jsn, exprs, select_gddp_experiment, lab='scenarios')
   vrstats = set_stat(jsn, vrs, select_gddp_var, lab='variables')
#summarize_size

#---------------------------------------------------------------------------------
if __name__ == '__main__':
   logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)
   thrds = []
   queue, JSN = queue_list( GDDP_INVENTORY )
   for i in range(NTRHS):
      t = threading.Thread(target=get_sizes)
      t.daemon = False
      t.start()
      thrds.append(t)
   for t in thrds: 
      t.join()

   summarize_size(JSN)
#__main__

