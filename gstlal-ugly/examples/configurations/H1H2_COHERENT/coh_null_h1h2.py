#!/usr/bin/evn python

import os
import sys
import numpy

import gobject
gobject.threads_init()
import pygtk
pygtk.require('2.0')
import pygst
pygst.require('0.10')
import gst
from optparse import OptionParser

from gstlal import pipeparts
from gstlal import lloidparts
from gstlal import reference_psd
from gstlal import coherent_null
from gstlal import inspiral

from glue import segments
from pylal.datatypes import LIGOTimeGPS
from pylal.xlal.datatypes.real8frequencyseries import REAL8FrequencySeries

#
# parse command line
#

parser = OptionParser()
parser.add_option("--frame-cache", metavar = "fileanme", help = "Set the name of the LAL cache listing the LIGO-Virgo .gwf frame files (optional).  This is required unless --fake-data or --online-data is used in which case it must not be set.")
parser.add_option("--fake-data", metavar = "[LIGO|AdvLIGO]", help = "Instead of reading data from .gwf files, generate and process coloured Gaussian noise modelling the Initial LIGO design spectrum (optional).")
parser.add_option("--gps-start-time", metavar = "seconds", help = "Set the start time of the segment to analyze in GPS seconds (required).  Can be specified to nanosecond precision.")
parser.add_option("--gps-end-time", metavar = "seconds", help = "Set the end time of the segment to analyze in GPS seconds (required).  Can be specified to nanosecond precision.")
parser.add_option("--injections", metavar = "filename", help = "Set the name of the LIGO light-weight XML file from which to load injections (optional).")
parser.add_option("--channel-name", metavar = "name", action = "append", default = "LSC-STRAIN", help = "Set the name of the channel to process (optional).  The default is \"LSC-STRAIN\".")
parser.add_option("-v", "--verbose", action = "store_true", help = "Be verbose (optional).")
parser.add_option("--write-pipeline", metavar = "filename", help = "Write a DOT graph description of the as-built pipeline to this file (optional).  The environment variable GST_DEBUG_DUMP_DOT_DIR must be set for this option to work.")
parser.add_option("--track-psd", action = "store_true", help = "Track the H1 and H2 psds.  The filters that create the coherent stream will be updated throughout the run.")
parser.add_option("--reference-psd", metavar = "filename", help = ".xml file containing the H1 and H2 psds. Use this psd as first guess psd or fixed psd.")
parser.add_option("--write-psd", metavar = "filename", help = "Measure psds and write to .xml file.  Use these psds as a first guess psd or fixed psd.")

options, filenames = parser.parse_args()

if len([option for option in ("frame_cache", "fake_data") if getattr(options, option) is not None]) != 1:
	raise ValueError("must provide either --frame-cache or --fake-data")

if options.fake_data not in (None, "LIGO", "AdvLIGO"):
	raise ValueError("unrecognized value for --fake-data %s" % options.fake_data)

if options.reference_psd is None and options.write_psd is None and options.track_psd is None:
	raise ValueError("must use --track-psd if --reference-psd or --write-psd are not given; you can use both simultaneously")

if len([option for option in ("reference_psd", "write_psd") if getattr(options, option) is not None]) != 1 and len([option for option in ("reference_psd", "write_psd") if getattr(options, option) is not None]) != 0:
	raise ValueError("must provide only one of --reference-psd or --write-psd")

required_options = ["gps_start_time", "gps_end_time", "channel_name"]

missing_options = []
missing_options += ["--%s" % option.replace("_", "-") for option in required_options if getattr(options, option) is None]
if missing_options:
	raise ValueError("missing required option(s) %s" % ", ".join(sorted(missing_options)))

options.seg = segments.segment(LIGOTimeGPS(options.gps_start_time), LIGOTimeGPS(options.gps_end_time))
options.psd_fft_length = 8
options.zero_pad_length = 2
options.srate = 4096
quality = 9

channel_dict = inspiral.channel_dict_from_channel_list(options.channel_name)

#
# =============================================================================
#
#                                   Handler Class
#
# =============================================================================
#

class COHhandler(lloidparts.LLOIDHandler):
	
	def __init__(self, mainloop, pipeline):

		# set various properties for psd and fir filter		
		self.psd_fft_length = options.psd_fft_length
		self.zero_pad_length = options.zero_pad_length		
		self.srate = options.srate
		self.filter_length = int(self.srate*self.psd_fft_length)

		# set default psds for H1 and H2
		self.psd1 = self.build_default_psd(self.srate, self.filter_length)
		self.psd2 = self.build_default_psd(self.srate, self.filter_length)

		# set default impulse response and latency for H1 and H2
		self.H1_impulse, self.H1_latency, self.H2_impulse, self.H2_latency, self.srate = coherent_null.psd_to_impulse_response(self.psd1, self.psd2)
		self.H1_impulse = numpy.zeros(len(self.H1_impulse))
		self.H2_impulse = numpy.zeros(len(self.H2_impulse))
		
		# psd1_change and psd2_change store when the psds have been updated
		self.psd1_change = 0
		self.psd2_change = 0

		# coherent null bin default
		self.cohnullbin = None

		# regular handler pipeline and message processing
		self.mainloop = mainloop
		self.pipeline = pipeline
		bus = pipeline.get_bus()
		bus.add_signal_watch()
		bus.connect("message", self.on_message)

	def on_message(self, bus, message):
		if message.type == gst.MESSAGE_EOS:
			self.pipeline.set_state(gst.STATE_NULL)
			self.mainloop.quit()
		elif message.type == gst.MESSAGE_ERROR:
			gerr, dbgmsg = message.parse_error()
			self.pipeline.set_state(gst.STATE_NULL)
			self.mainloop.quit()
			sys.exit("error (%s:%d '%s'): %s" % (gerr.domain, gerr.code, gerr.message, dbgmsg))

	def build_default_psd(self, srate, filter_length):
		psd = REAL8FrequencySeries()
		psd.deltaF = float(srate)/filter_length
		psd.data = numpy.ones(filter_length/2 + 1)
		psd.f0 = 0.0
		return psd

	def add_cohnull_bin(self, elem):
		self.cohnullbin = elem
	
	def update_fir_filter(self):
		self.psd1_change = 0
		self.psd2_change = 0
		self.H1_impulse, self.H1_latency, self.H2_impulse, self.H2_latency, self.srate = coherent_null.psd_to_impulse_response(self.psd1, self.psd2)
		self.cohnullbin.set_property("block-stride", self.srate)
		self.cohnullbin.set_property("H1-impulse", self.H1_impulse)
		self.cohnullbin.set_property("H2-impulse", self.H2_impulse)
		self.cohnullbin.set_property("H1-latency", self.H1_latency)
		self.cohnullbin.set_property("H2-latency", self.H2_latency)		
		
	def update_psd1(self, elem):
		self.psd1 = REAL8FrequencySeries(
			name = "PSD1",
			f0 = 0.0,
			deltaF = elem.get_property("delta-f"),
			data = numpy.array(elem.get_property("mean-psd"))
		)
		self.psd1_change = 1

	def update_psd2(self, elem):
		self.psd2 = REAL8FrequencySeries(
			name = "PSD2",
			f0 = 0.0,
			deltaF = elem.get_property("delta-f"),
			data = numpy.array(elem.get_property("mean-psd"))
		)
		self.psd2_change = 1
	
	def fixed_filters(self, psd1, psd2):
		self.psd1 = REAL8FrequencySeries(
			name = "PSD1",
			f0 = 0.0,
			deltaF = psd1.deltaF,
			data = psd1.data)
		self.psd2 = REAL8FrequencySeries(
			name = "PSD2",
			f0 = 0.0,
			deltaF = psd2.deltaF,
			data = psd2.data)
		self.H1_impulse, self.H1_latency, self.H2_impulse, self.H2_latency, self.srate = coherent_null.psd_to_impulse_response(self.psd1, self.psd2)


#
# =============================================================================
#
#                                             Main
#
# =============================================================================
#

#
# set up source information
#

if options.gps_start_time is None:
	seek_start_type = gst.SEEK_TYPE_NONE
	seek_start_time = -1 # gst.CLOCK_TIME_NONE is exported as unsigned, should have been signed
else:
	seek_start_type = gst.SEEK_TYPE_SET
	seek_start_time = options.seg[0].ns()

if options.gps_end_time is None:
	seek_stop_type = gst.SEEK_TYPE_NONE
	seek_stop_time = -1 # gst.CLOCK_TIME_NONE is exported as unsigned, should have been signed
else:
	seek_stop_type = gst.SEEK_TYPE_SET
	seek_stop_time = options.seg[1].ns()

seekevent = gst.event_new_seek(1.0, gst.Format(gst.FORMAT_TIME), gst.SEEK_FLAG_FLUSH | gst.SEEK_FLAG_KEY_UNIT, seek_start_type, seek_start_time, seek_stop_type, seek_stop_time)

detectors = {}

detectors["H1"] = lloidparts.DetectorData(options.frame_cache, channel_dict["H1"])
detectors["H2"] = lloidparts.DetectorData(options.frame_cache, channel_dict["H2"])

if options.reference_psd is not None:
	psd = reference_psd.read_psd(options.reference_psd, verbose = options.verbose)
	psd1 = psd["H1"]
	psd2 = psd["H2"]
if options.write_psd is not None:
	psd1 = reference_psd.measure_psd(
		"H1",
		seekevent,
		detectors["H1"],
		options.seg,
		options.srate,
		psd_fft_length = options.psd_fft_length,
		fake_data = options.fake_data,
		injection_filename = options.injections,
		verbose = options.verbose
		)
	psd2 = reference_psd.measure_psd(
		"H2",
		seekevent,
		detectors["H2"],
		options.seg,
		options.srate,
		psd_fft_length = options.psd_fft_length,
		fake_data = options.fake_data,
		injection_filename = options.injections,
		verbose = options.verbose
		)
	reference_psd.write_psd(options.write_psd, { "H1" : psd1, "H2" : psd2 }, verbose = options.verbose)

#
# this function updates the psds and recalucates the fir filters used in the coherent stream
#	

#FIXME: Probably only want to update psds (and filters) when they have changed "significantly enough"
def update_psd(elem, pspec, hand):
	name = elem.get_property("name")
	if name == "H1_whitener":
		hand.update_psd1(elem)
	if name == "H2_whitener":
		hand.update_psd2(elem)
	if (hand.psd1_change == 1 and hand.psd2_change == 1):
		hand.update_fir_filter()

#
# begin pipline
#

pipeline = gst.Pipeline("coh_null_h1h2")
mainloop = gobject.MainLoop()
handler = COHhandler(mainloop, pipeline)

if options.reference_psd is not None or options.write_psd is not None:
	handler.fixed_filters(psd1, psd2)

#
# H1 branch
#

H1src = lloidparts.mkLLOIDbasicsrc(
	pipeline,
	seekevent,
	"H1",
	detectors["H1"],
	data_source = options.fake_data or "frames",
	injection_filename = options.injections,
	verbose = options.verbose
)

H1head = pipeparts.mkreblock(pipeline, H1src)
H1head = pipeparts.mkcapsfilter(pipeline, H1head, "audio/x-raw-float, width=64, rate=[%d,MAX]" % handler.srate)
H1head = pipeparts.mkresample(pipeline, H1head, quality = quality)
H1head = pipeparts.mkcapsfilter(pipeline, H1head, "audio/x-raw-float, width=64, rate=%d" % handler.srate)

# tee off for null veto later on
H1vetotee = pipeparts.mktee(pipeline, H1head)
H1head = pipeparts.mkqueue(pipeline, H1vetotee)

# track psd
if options.track_psd is not None:
	H1psdtee = pipeparts.mktee(pipeline, H1head)
	H1psd = pipeparts.mkchecktimestamps(pipeline, H1psdtee)
	H1psd = pipeparts.mkwhiten(pipeline, H1psd, zero_pad = handler.zero_pad_length, fft_length = handler.psd_fft_length, name = "H1_whitener") 
	H1psd.connect_after("notify::mean-psd", update_psd, handler)
	H1psd = pipeparts.mkchecktimestamps(pipeline, H1psd)
	pipeparts.mkfakesink(pipeline, H1psd)

	H1head = H1psdtee

#
# H2 branch
#

H2src = lloidparts.mkLLOIDbasicsrc(
	pipeline,
	seekevent,
	"H2",
	detectors["H2"],
	data_source = options.fake_data or "frames",
	injection_filename = options.injections,
	verbose = options.verbose
)

H2head = pipeparts.mkreblock(pipeline, H2src)
H2head = pipeparts.mkcapsfilter(pipeline, H2head, "audio/x-raw-float, rate=[%d,MAX]" % handler.srate)
H2head = pipeparts.mkresample(pipeline, H2head, quality = quality)
H2head = pipeparts.mkcapsfilter(pipeline, H2head, "audio/x-raw-float, rate=%d" % handler.srate)

# tee off for null veto later on
H2vetotee = pipeparts.mktee(pipeline, H2head)
H2head = pipeparts.mkqueue(pipeline, H2vetotee)

# track psd
if options.track_psd is not None:
	H2psdtee = pipeparts.mktee(pipeline, H2head)
	H2psd = pipeparts.mkchecktimestamps(pipeline, H2psdtee)
	H2psd = pipeparts.mkwhiten(pipeline, H2psd, zero_pad = handler.zero_pad_length, fft_length = handler.psd_fft_length, name = "H2_whitener")
	H2psd.connect_after("notify::mean-psd", update_psd, handler)
	H2psd = pipeparts.mkchecktimestamps(pipeline, H2psd)
	H2psd = pipeparts.mkfakesink(pipeline, H2psd)
	
	H2head = H2psdtee

#
# Create coherent and null streams
#

coherent_null_bin = pipeparts.mklhocoherentnull(pipeline, H1head, H2head, handler.H1_impulse, handler.H1_latency, handler.H2_impulse, handler.H2_latency, handler.srate)
if options.track_psd is not None:
	handler.add_cohnull_bin(coherent_null_bin)
cohhead = coherent_null_bin.get_pad("COHsrc")
nullhead = coherent_null_bin.get_pad("NULLsrc")

#
# Create coherent and null frames
#

cohhead = pipeparts.mkprogressreport(pipeline, cohhead, "progress_coherent")
pipeparts.mkframesink(pipeline, cohhead, clean_timestamps = False, dir_digits = 0, frame_type = "H1_LHO_COHERENT")

# null veto
nullhead = pipeparts.mkprogressreport(pipeline, nullhead, "progress_null")
nullhead = pipeparts.mkwhiten(pipeline, nullhead, zero_pad = handler.zero_pad_length, fft_length = handler.psd_fft_length)
pipeparts.mkfakesink(pipeline, nullhead)
#nulltee = pipeparts.mktee(pipeline, nullhead)

#H1vetohead = pipeparts.mkgate(pipeline, H1vetotee, threshold = 100, control = nulltee, invert_control = True)
#pipeparts.mkfakesink(pipeline, H1vetohead)

#H2vetohead = pipeparts.mkgate(pipeline, H2vetotee, threshold = 8, control = nulltee, invert_control = True)
#pipeparts.mkfakesink(pipeline, H2vetohead)

#
# Running the pipeline messages and pipeline graph
#

def write_dump_dot(pipeline, filestem, verbose = False):
	if "GST_DEBUG_DUMP_DOT_DIR" not in os.environ:
		raise ValueError, "cannot write pipeline, environment variable GST_DEBUG_DUMP_DOT_DIR is not set"
	gst.DEBUG_BIN_TO_DOT_FILE(pipeline, gst.DEBUG_GRAPH_SHOW_ALL, filestem)
	if verbose:
		print >>sys.stderr, "Wrote pipeline to %s" % os.path.join(os.environ["GST_DEBUG_DUMP_DOT_DIR"], "%s.dot" % filestem)

if options.write_pipeline is not None:
	write_dump_dot(pipeline, "%s.%s" % (options.write_pipeline, "NULL"), verbose = options.verbose)

if options.verbose:
	print >>sys.stderr, "setting pipeline state to paused ..."
if pipeline.set_state(gst.STATE_PAUSED) != gst.STATE_CHANGE_SUCCESS:
	raise RuntimeError, "pipeline did not enter paused state"

if options.verbose:
	print >>sys.stderr, "setting pipeline state to playing ..."
if pipeline.set_state(gst.STATE_PLAYING) != gst.STATE_CHANGE_SUCCESS:
	raise RuntimeError, "pipeline did not enter playing state"

if options.write_pipeline is not None:
	write_dump_dot(pipeline, "%s.%s" % (options.write_pipeline, "PLAYING"), verbose = options.verbose)

if options.verbose:
	print >>sys.stderr, "running pipeline ..."

mainloop.run()
