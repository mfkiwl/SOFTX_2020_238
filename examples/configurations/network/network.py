#!/usr/bin/env python
__author__ = "Leo Singer <leo.singer@ligo.org>"


from optparse import Option, OptionParser
opts, args = OptionParser(
	option_list =
	[
		Option("--instrument", "-i", metavar="IFO", action="append", help="Instruments to analyze."),
		Option("--gps-start-time", "-s", metavar="INT", type="int", help="GPS time at which to start analysis."),
		Option("--gps-end-time", "-e", metavar="INT", type="int", help="GPS time at which to end analysis."),
		Option("--template-bank", metavar="FILE", help="Name of template bank file."),
		Option("--output", metavar="FILE", help="Name of output file. (.xml .xml.gz or .sqlite)"),
	]
).parse_args()


opts.psd_fft_length = 8


from gstlal.gstlal_svd_bank import read_bank
from gstlal.gstlal_reference_psd import read_psd
from gstlal import lloidparts
from gstlal.pipeutil import gst, gobject
from gstlal.lloidparts import mkelems_fast
from gstlal import ligolw_output
from glue import segments
from pylal.datatypes import LIGOTimeGPS
import sys


# FIXME: This class, or something like it, occurs in many of our pipelines.
# Maybe we could put a generalized version of it in a library?
class LLOIDHandler(object):
	def __init__(self, mainloop, pipeline):
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


pipeline = gst.Pipeline()
mainloop = gobject.MainLoop()
handler = LLOIDHandler(mainloop, pipeline)


seekevent = gst.event_new_seek(
	1.0, gst.FORMAT_TIME, gst.SEEK_FLAG_KEY_UNIT,
	gst.SEEK_TYPE_SET, opts.gps_start_time * gst.SECOND,
	gst.SEEK_TYPE_SET, opts.gps_end_time * gst.SECOND
)


coinc = mkelems_fast(pipeline, "lal_coinc")[-1]
#skymap = mkelems_fast(pipeline, coinc, "lal_skymap", {"bank-filename": opts.template_bank})[-1]
#mkelems_fast(pipeline, skymap, "fakesink")
#mkelems_fast(pipeline, coinc, "fakesink")

seg = segments.segment(LIGOTimeGPS(opts.gps_start_time), LIGOTimeGPS(opts.gps_end_time))
data = {}
for ifo in opts.instrument:
	bank = read_bank("bank.%s.pickle" % ifo)
	bank.logname = ifo # FIXME This is only need to give elements names, that should be automatic.
	psd = read_psd("reference_psd.%s.xml.gz" % ifo)
	rates = bank.get_rates()

	basicsrc = lloidparts.mkLLOIDbasicsrc(pipeline, seekevent, ifo, None, online_data=True)
	hoftdict = lloidparts.mkLLOIDsrc(pipeline, basicsrc, rates, psd=psd, psd_fft_length=opts.psd_fft_length)
	snr_tee = lloidparts.mkLLOIDhoftToSnr(pipeline, hoftdict, ifo, bank, lloidparts.mkcontrolsnksrc(pipeline, max(rates)))
	triggers = lloidparts.mkLLOIDsnrToTriggers(pipeline, snr_tee, bank, lal_triggergen_algorithm=1, lal_triggergen_max_gap=0.1)
	triggers_tee = mkelems_fast(pipeline, triggers, "tee")[-1]
	# output a database for each detector
	data[ifo] = ligolw_output.Data([ifo], tmp_space=None, output=ifo+"-"+opts.output, seg=seg, out_seg=seg, injections=None, comment="", verbose=True)
	data[ifo].prepare_output_file(ligolw_output.make_process_params(opts))
	mkelems_fast(pipeline, triggers_tee, "appsink", {"caps": gst.Caps("application/x-lal-snglinspiral"), "sync": False, "async": False, "emit-signals": True, "max-buffers": 1, "drop": True})[-1].connect_after("new-buffer", ligolw_output.appsink_new_buffer, data[ifo])
	triggers_tee.link_pads("src%d", coinc, "sink%d")
	#mkelems_fast(pipeline, snr_tee, "queue", skymap)

#
# output file
#

# FIXME make some of these kw args options
data['all'] = ligolw_output.Data(opts.instrument, tmp_space=None, output="".join(opts.instrument)+"-"+opts.output, seg=seg, out_seg=seg, injections=None, comment="", verbose=True)
data['all'].prepare_output_file(ligolw_output.make_process_params(opts))
mkelems_fast(pipeline, coinc, "appsink", {"caps": gst.Caps("application/x-lal-snglinspiral"), "sync": False, "async": False, "emit-signals": True, "max-buffers": 1, "drop": True})[-1].connect_after("new-buffer", ligolw_output.appsink_new_buffer, data['all'])

#
# Ready set go!
#

pipeline.set_state(gst.STATE_PLAYING)
gst.DEBUG_BIN_TO_DOT_FILE(pipeline, gst.DEBUG_GRAPH_SHOW_NON_DEFAULT_PARAMS, "network")
mainloop.run()
data.write_output_file()
