#!/usr/bin/env python

from gnuradio import gr, gru, optfir, eng_notation, blks2, air
from gnuradio import uhd
from gnuradio.eng_option import eng_option
from optparse import OptionParser
import time, os, sys
from string import split, join
from usrpm import usrp_dbid
from modes_print import modes_output_print
from modes_sql import modes_sql
from modes_sbs1 import modes_output_sbs1
import gnuradio.gr.gr_threading as _threading

class top_block_runner(_threading.Thread):
    def __init__(self, tb):
        _threading.Thread.__init__(self)
        self.setDaemon(1)
        self.tb = tb
        self.done = False
        self.start()

    def run(self):
        self.tb.run()
        self.done = True


class adsb_rx_block (gr.top_block):

  def __init__(self, options, args, queue):
    gr.top_block.__init__(self)

    self.options = options
    self.args = args

    if options.filename is None:
      self.u = uhd.simple_source("", uhd.io_type_t.COMPLEX_FLOAT32)

      if(options.rx_subdev_spec is None):
        options.rx_subdev_spec = ""
      self.u.set_subdev_spec(options.rx_subdev_spec)

      rate = options.rate
      self.u.set_samp_rate(rate)
      rate = int(self.u.get_samp_rate()) #retrieve actual

      if options.gain is None: #set to halfway
        g = self.u.get_gain_range()
        options.gain = (g.min+g.max) / 2.0

      if not(self.tune(options.freq)):
        print "Failed to set initial frequency"

      print "Setting gain to %i" % (options.gain,)
      self.u.set_gain(options.gain)

    else:
      rate = options.rate
      self.u = gr.file_source(gr.sizeof_gr_complex, options.filename)

    print "Rate is %i" % (rate,)
    print "Gain is %i" % (self.u.get_gain(),)
    pass_all = 0
    if options.output_all :
      pass_all = 1

    self.demod = gr.complex_to_mag()
    self.avg = gr.moving_average_ff(100, 1.0/100, 400);
    self.preamble = air.modes_preamble(rate, options.threshold)
    self.framer = air.modes_framer(rate)
    self.slicer = air.modes_slicer(rate, queue)

    self.connect(self.u, self.demod)
    self.connect(self.demod, self.avg)
    self.connect(self.demod, (self.preamble, 0))
    self.connect(self.avg, (self.preamble, 1))
    self.connect(self.demod, (self.framer, 0))
    self.connect(self.preamble, (self.framer, 1))
    self.connect(self.demod, (self.slicer, 0))
    self.connect(self.framer, (self.slicer, 1))

  def tune(self, freq):
    result = self.u.set_center_freq(freq)
    return result
    
def post_to_sql(db, query):
  if query is not None:
    c = db.cursor()
    c.execute(query)

if __name__ == '__main__':
  usage = "%prog: [options] output filename"
  parser = OptionParser(option_class=eng_option, usage=usage)
  parser.add_option("-R", "--rx-subdev-spec", type="string",
          help="select USRP Rx side A or B", metavar="SUBDEV")
  parser.add_option("-f", "--freq", type="eng_float", default=1090e6,
                      help="set receive frequency in Hz [default=%default]", metavar="FREQ")
  parser.add_option("-g", "--gain", type="int", default=None,
                      help="set RF gain", metavar="dB")
  parser.add_option("-r", "--rate", type="int", default=4000000,
                      help="set ADC sample rate [default=%default]")
  parser.add_option("-T", "--threshold", type="eng_float", default=3.0,
                      help="set pulse detection threshold above noise in dB [default=%default]")
  parser.add_option("-a","--output-all", action="store_true", default=False,
                      help="output all frames")
  parser.add_option("-b","--bandwidth", type="eng_float", default=5e6,
          help="set DBSRX baseband bandwidth in Hz [default=%default]")
  parser.add_option("-F","--filename", type="string", default=None,
            help="read data from file instead of USRP")
  parser.add_option("-D","--database", action="store_true", default=False,
                      help="send to database instead of printing to screen")
  parser.add_option("-P","--sbs1", action="store_true", default=False,
                      help="open an SBS-1-compatible server on port 30003")
  parser.add_option("-n","--no-print", action="store_true", default=False,
                      help="disable printing decoded packets to stdout")
  (options, args) = parser.parse_args()


  queue = gr.msg_queue()
  
  outputs = [] #registry of plugin output functions
  updates = [] #registry of plugin update functions

  if options.database is True:
    import pysqlite3
    #db = pysqlite3.connect(:memory:)
    #here we have to initialize the database with the correct tables and relations
    
  if options.sbs1 is True:
    sbs1port = modes_output_sbs1()
    outputs.append(sbs1port.output)
    updates.append(sbs1port.add_pending_conns)
    
  if options.no_print is not True:
    outputs.append(modes_output_print().parse)

  fg = adsb_rx_block(options, args, queue)
  runner = top_block_runner(fg)

  while 1:
    try:
      #the update registry is really for the SBS1 plugin -- we're looking for new TCP connections.
      #i think we have to do this here rather than in the output handler because otherwise connections will stack up
      #until the next output
      for update in updates:
        update()
      
      #main message handler
      if queue.empty_p() == 0 :
        while queue.empty_p() == 0 :
          msg = queue.delete_head() #blocking read

	  for out in outputs:
	    out(msg.to_string())

      elif runner.done:
        raise KeyboardInterrupt
      else:
        time.sleep(0.1)

    except KeyboardInterrupt:
      fg.stop()
      runner = None
      break