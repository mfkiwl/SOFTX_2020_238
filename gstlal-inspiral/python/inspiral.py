# Copyright (C) 2009-2013  Kipp Cannon, Chad Hanna, Drew Keppel
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#

## @file
# The python module to implement things needed by gstlal_inspiral
#
# ### Review Status
#
# STATUS: reviewed with actions
#
# | Names                                          | Hash                                        | Date        | Diff to Head of Master     |
# | -------------------------------------------    | ------------------------------------------- | ----------  | -------- |
# | Kipp Cannon, Chad Hanna, Jolien Creighton, Florent Robinet, B. Sathyaprakash, Duncan Meacher, T.G.G. Li    | b8fef70a6bafa52e3e120a495ad0db22007caa20 | 2014-12-03 | <a href="@gstlal_inspiral_cgit_diff/python/inspiral.py?id=HEAD&id2=b8fef70a6bafa52e3e120a495ad0db22007caa20">inspiral.py</a> |
# | Kipp Cannon, Chad Hanna, Jolien Creighton, B. Sathyaprakash, Duncan Meacher                                | 72875f5cb241e8d297cd9b3f9fe309a6cfe3f716 | 2015-11-06 | <a href="@gstlal_inspiral_cgit_diff/python/inspiral.py?id=HEAD&id2=72875f5cb241e8d297cd9b3f9fe309a6cfe3f716">inspiral.py</a> |
#
# #### Action items
# - Document examples of how to get SNR history, etc., to a web browser in an offline search
# - Long term goal: Using template duration (rather than chirp mass) should load balance the pipeline and improve statistics
# - L651: One thing to sort out is the signal probability while computing coincs
# - L640-L647: Get rid of obsolete comments
# - L667: Make sure timeslide events are not sent to GRACEDB
# - Lxxx: Can normalisation of the tail of the distribution pre-computed using fake data?
# - L681: fmin should not be hard-coded to 10 Hz. horizon_distance will be horribly wrong if psd is constructed, e.g. using some high-pass filter. For example, change the default to 40 Hz.
# - L817: If gracedb upload failed then it should be possible to identify the failure, the specifics of the trigger that encountered failure and a way of submitting the trigger again to gracedb is important. Think about how to clean-up failures.
# - Mimick gracedb upload failures and see if the code crashes


## @package inspiral

#
# =============================================================================
#
#                                   Preamble
#
# =============================================================================
#


import bisect
from collections import deque
import itertools
import math
import numpy
import os
import resource
from scipy import random
import sqlite3
import StringIO
import subprocess
import sys
import threading
import time
import httplib
import tempfile
import shutil

import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst
GObject.threads_init()
Gst.init(None)

try:
	from ligo import gracedb
except ImportError:
	print >>sys.stderr, "warning: gracedb import failed, program will crash if gracedb uploads are attempted"

from glue import iterutils
from glue import segments
from glue import segmentsUtils
from glue.ligolw import ligolw
from glue.ligolw import dbtables
from glue.ligolw import ilwd
from glue.ligolw import lsctables
from glue.ligolw import array as ligolw_array
from glue.ligolw import param as ligolw_param
from glue.ligolw import utils as ligolw_utils
from glue.ligolw.utils import ligolw_sqlite
from glue.ligolw.utils import ligolw_add
from glue.ligolw.utils import process as ligolw_process
from glue.ligolw.utils import segments as ligolw_segments
from glue.ligolw.utils import time_slide as ligolw_time_slide
import lal
from lal import LIGOTimeGPS
from lal import rate
from lal import series as lalseries
from lal.utils import CacheEntry

from gstlal import bottle
from gstlal import reference_psd
from gstlal import streamthinca
from gstlal import svd_bank
from gstlal import cbc_template_iir
from gstlal import far


#
# =============================================================================
#
#                         glue.ligolw Content Handlers
#
# =============================================================================
#


@ligolw_array.use_in
@ligolw_param.use_in
@lsctables.use_in
class LIGOLWContentHandler(ligolw.LIGOLWContentHandler):
	pass


#
# =============================================================================
#
#                                     Misc
#
# =============================================================================
#


def now():
	return LIGOTimeGPS(lal.UTCToGPS(time.gmtime()))


def message_new_checkpoint(src, timestamp = None):
	s = Gst.Structure.new_empty("CHECKPOINT")
	message = Gst.Message.new_application(src, s)
	if timestamp is not None:
		message.timestamp = timestamp
	return message


def state_vector_on_off_dict_from_bit_lists(on_bit_list, off_bit_list, state_vector_on_off_dict = {"H1" : [0x7, 0x160], "L1" : [0x7, 0x160], "V1" : [0x67, 0x100]}):
	"""
	"""

	for line in on_bit_list:
		ifo = line.split("=")[0]
		bits = "".join(line.split("=")[1:])
		try:
			state_vector_on_off_dict[ifo][0] = int(bits)
		except ValueError: # must be hex
			state_vector_on_off_dict[ifo][0] = int(bits, 16)
	
	for line in off_bit_list:
		ifo = line.split("=")[0]
		bits = "".join(line.split("=")[1:])
		try:
			state_vector_on_off_dict[ifo][1] = int(bits)
		except ValueError: # must be hex
			state_vector_on_off_dict[ifo][1] = int(bits, 16)

	return state_vector_on_off_dict


def state_vector_on_off_list_from_bits_dict(bit_dict):
	"""
	"""

	onstr = ""
	offstr = ""
	for i, ifo in enumerate(bit_dict):
		if i == 0:
			onstr += "%s=%s " % (ifo, bit_dict[ifo][0])
			offstr += "%s=%s " % (ifo, bit_dict[ifo][1])
		else:
			onstr += "--state-vector-on-bits=%s=%s " % (ifo, bit_dict[ifo][0])
			offstr += "--state-vector-off-bits=%s=%s " % (ifo, bit_dict[ifo][1])

	return onstr, offstr


def parse_svdbank_string(bank_string):
	"""
	parses strings of form 
	
	H1:bank1.xml,H2:bank2.xml,L1:bank3.xml
	
	into a dictionary of lists of bank files.
	"""
	out = {}
	if bank_string is None:
		return out
	for b in bank_string.split(','):
		ifo, bank = b.split(':')
		if ifo in out:
			raise ValueError("Only one svd bank per instrument should be given")
		out[ifo] = bank
	return out


def parse_iirbank_string(bank_string):
	"""
	parses strings of form 
	
	H1:bank1.xml,H2:bank2.xml,L1:bank3.xml,H2:bank4.xml,... 
	
	into a dictionary of lists of bank files.
	"""
	out = {}
	if bank_string is None:
		return out
	for b in bank_string.split(','):
		ifo, bank = b.split(':')
		out.setdefault(ifo, []).append(bank)
	return out


def parse_bank_files(svd_banks, verbose, snr_threshold = None):
	"""
	given a dictionary of lists of svd template bank file names parse them
	into a dictionary of bank classes
	"""

	banks = {}

	for instrument, filename in svd_banks.items():
		for n, bank in enumerate(svd_bank.read_banks(filename, contenthandler = LIGOLWContentHandler, verbose = verbose)):
			# Write out sngl inspiral table to temp file for
			# trigger generator
			# FIXME teach the trigger generator to get this
			# information a better way
			bank.template_bank_filename = tempfile.NamedTemporaryFile(suffix = ".gz", delete = False).name
			xmldoc = ligolw.Document()
			# FIXME if this table reference is from a DB this
			# is a problem (but it almost certainly isn't)
			xmldoc.appendChild(ligolw.LIGO_LW()).appendChild(bank.sngl_inspiral_table.copy()).extend(bank.sngl_inspiral_table)
			ligolw_utils.write_filename(xmldoc, bank.template_bank_filename, gz = True, verbose = verbose)
			xmldoc.unlink()	# help garbage collector
			bank.logname = "%sbank%d" % (instrument, n)
			banks.setdefault(instrument, []).append(bank)
			if snr_threshold is not None:
				bank.snr_threshold = snr_threshold

	# FIXME remove when this is no longer an issue
	if not banks:
		raise ValueError("Could not parse bank files into valid bank dictionary.\n\t- Perhaps you are using out-of-date svd bank files?  Please ensure that they were generated with the same code version as the parsing code")
	return banks

def parse_iirbank_files(iir_banks, verbose, snr_threshold = 4.0):
	"""
	given a dictionary of lists of iir template bank file names parse them
	into a dictionary of bank classes
	"""

	banks = {}

	for instrument, files in iir_banks.items():
		for n, filename in enumerate(files):
			# FIXME over ride the file name stored in the bank file with
			# this file name this bank I/O code needs to be fixed
			bank = cbc_template_iir.load_iirbank(filename, snr_threshold, contenthandler = LIGOLWContentHandler, verbose = verbose)
			bank.template_bank_filename = filename
			bank.logname = "%sbank%d" % (instrument,n)
			banks.setdefault(instrument,[]).append(bank)

	return banks


def subdir_from_T050017_filename(fname):
	path = str(CacheEntry.from_T050017(fname).segment[0])[:5]
	try:
		os.mkdir(path)
	except OSError:
		pass
	return path


def lower_bound_in_seglist(seglist, x):
	"""
	Return the index of the segment immediately at or before x in the
	coalesced segment list seglist.  Operation is O(log n).
	"""
	# NOTE:  this is an operation that is performed in a number of
	# locations in various codes, and is something that I've screwed up
	# more than once.  maybe this should be put into segments.py itself
	i = bisect.bisect_right(seglist, x)
	return i - 1 if i else 0


#
# =============================================================================
#
#                           Parameter Distributions
#
# =============================================================================
#


#
# Functions to synthesize injections
#


def snr_distribution(size, startsnr):
	"""
	This produces a power law distribution in snr of size size starting at startsnr
	"""
	return startsnr * random.power(3, size)**-1 # 3 here actually means 2 :) according to scipy docs


def noncentrality(snrs, prefactor):
	"""
	This produces a set of noncentrality parameters that scale with snr^2 according to the prefactor
	"""
	return prefactor * random.rand(len(snrs)) * snrs**2 # FIXME power depends on dimensionality of the bank and the expectation for the mismatch for real signals
	#return prefactor * random.power(1, len(snrs)) * snrs**2 # FIXME power depends on dimensionality of the bank and the expectation for the mismatch for real signals


def chisq_distribution(df, non_centralities, size):
	"""
	This produces a set of noncentral chisq values of size size, with degrees of freedom given by df
	"""
	out = numpy.empty((len(non_centralities) * size,))
	for i, nc in enumerate(non_centralities):
		out[i*size:(i+1)*size] = random.noncentral_chisquare(df, nc, size)
	return out


#
# =============================================================================
#
#                               Output Document
#
# =============================================================================
#


class CoincsDocument(object):
	sngl_inspiral_columns = ("process_id", "ifo", "end_time", "end_time_ns", "eff_distance", "coa_phase", "mass1", "mass2", "snr", "chisq", "chisq_dof", "bank_chisq", "bank_chisq_dof", "sigmasq", "spin1x", "spin1y", "spin1z", "spin2x", "spin2y", "spin2z", "template_duration", "event_id", "Gamma0", "Gamma1")

	def __init__(self, url, process_params, comment, instruments, seg, offsetvectors, injection_filename = None, tmp_path = None, replace_file = None, verbose = False):
		#
		# how to make another like us
		#

		self.get_another = lambda: CoincsDocument(url = url, process_params = process_params, comment = comment, instruments = instruments, seg = seg, offsetvectors = offsetvectors, injection_filename = injection_filename, tmp_path = tmp_path, replace_file = replace_file, verbose = verbose)

		#
		# url
		#

		self.url = url

		#
		# build the XML document
		#

		self.xmldoc = ligolw.Document()
		self.xmldoc.appendChild(ligolw.LIGO_LW())
		self.process = ligolw_process.register_to_xmldoc(self.xmldoc, u"gstlal_inspiral", process_params, comment = comment, ifos = instruments)
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.SnglInspiralTable, columns = self.sngl_inspiral_columns))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.CoincDefTable))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.CoincTable))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.CoincMapTable))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.TimeSlideTable))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.CoincInspiralTable))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.SegmentDefTable, columns = ligolw_segments.LigolwSegmentList.segment_def_columns))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.SegmentSumTable, columns = ligolw_segments.LigolwSegmentList.segment_sum_columns))
		self.xmldoc.childNodes[-1].appendChild(lsctables.New(lsctables.SegmentTable, columns = ligolw_segments.LigolwSegmentList.segment_columns))

		#
		# optionally insert injection list document
		#

		if injection_filename is not None:
			ligolw_add.ligolw_add(self.xmldoc, [injection_filename], contenthandler = LIGOLWContentHandler, verbose = verbose)

		#
		# insert time slide offset vectors.  remove duplicate
		# offset vectors when done
		#

		time_slide_table = lsctables.TimeSlideTable.get_table(self.xmldoc)
		for offsetvector in offsetvectors:
			time_slide_table.append_offsetvector(offsetvector, self.process)
		time_slide_mapping = ligolw_time_slide.time_slides_vacuum(time_slide_table.as_dict())
		iterutils.inplace_filter(lambda row: row.time_slide_id not in time_slide_mapping, time_slide_table)
		for tbl in self.xmldoc.getElementsByTagName(ligolw.Table.tagName):
			tbl.applyKeyMapping(time_slide_mapping)

		#
		# if the output is an sqlite database, build the sqlite
		# database and convert the in-ram XML document to an
		# interface to the database file
		#

		if url is not None and url.endswith('.sqlite'):
			self.working_filename = dbtables.get_connection_filename(ligolw_utils.local_path_from_url(url), tmp_path = tmp_path, replace_file = replace_file, verbose = verbose)
			self.connection = sqlite3.connect(self.working_filename, check_same_thread = False)
			ligolw_sqlite.insert_from_xmldoc(self.connection, self.xmldoc, preserve_ids = False, verbose = verbose)

			#
			# convert self.xmldoc into wrapper interface to
			# database
			#

			self.xmldoc.removeChild(self.xmldoc.childNodes[-1]).unlink()
			self.xmldoc.appendChild(dbtables.get_xml(self.connection))

			# recover the process_id following the ID remapping
			# that might have happened when the document was
			# inserted.  hopefully this query is unique enough
			# to find exactly the one correct entry in the
			# database

			(self.process.process_id,), = self.connection.cursor().execute("SELECT process_id FROM process WHERE program == ? AND node == ? AND username == ? AND unix_procid == ? AND start_time == ?", (self.process.program, self.process.node, self.process.username, self.process.unix_procid, self.process.start_time)).fetchall()
			self.process.process_id = ilwd.ilwdchar(self.process.process_id)
		else:
			self.connection = self.working_filename = None

		#
		# retrieve references to the table objects, now that we
		# know if they are database-backed or XML objects
		#

		self.sngl_inspiral_table = lsctables.SnglInspiralTable.get_table(self.xmldoc)


	def commit(self):
		# update output document
		if self.connection is not None:
			self.connection.commit()


	@property
	def process_id(self):
		return self.process.process_id


	def get_next_sngl_id(self):
		return self.sngl_inspiral_table.get_next_id()


	def write_output_url(self, seglistdicts = None, verbose = False):
		if seglistdicts is not None:
			with ligolw_segments.LigolwSegments(self.xmldoc, self.process) as llwsegments:
				for segtype, seglistdict in seglistdicts.items():
					llwsegments.insert_from_segmentlistdict(seglistdict, name = segtype, comment = "LLOID")

		ligolw_process.set_process_end_time(self.process)

		if self.connection is not None:
			# record the final state of the process row in the
			# database
			cursor = self.connection.cursor()
			cursor.execute("UPDATE process SET end_time = ? WHERE process_id == ?", (self.process.end_time, self.process.process_id))
			cursor.close()
			self.connection.commit()
			dbtables.build_indexes(self.connection, verbose = verbose)
			self.connection.close()
			self.connection = None
			dbtables.put_connection_filename(ligolw_utils.local_path_from_url(self.url), self.working_filename, verbose = verbose)
		else:
			self.sngl_inspiral_table.sort(key = lambda row: (row.end, row.ifo))
			ligolw_utils.write_url(self.xmldoc, self.url, gz = (self.url or "stdout").endswith(".gz"), verbose = verbose, trap_signals = None)
		# can no longer be used
		self.xmldoc.unlink()
		del self.xmldoc


class Data(object):
	def __init__(self, coincs_document, pipeline, rankingstat, zerolag_rankingstatpdf_url = None, rankingstatpdf_url = None, ranking_stat_output_url = None, ranking_stat_input_url = None, likelihood_snapshot_interval = None, thinca_interval = 50.0, min_log_L = None, sngls_snr_threshold = None, gracedb_far_threshold = None, gracedb_min_instruments = None, gracedb_group = "Test", gracedb_search = "LowMass", gracedb_pipeline = "gstlal", gracedb_service_url = "https://gracedb.ligo.org/api/", upload_auxiliary_data_to_gracedb = True, verbose = False):
		#
		# initialize
		#

		self.lock = threading.Lock()
		self.coincs_document = coincs_document
		self.pipeline = pipeline
		self.verbose = verbose
		self.upload_auxiliary_data_to_gracedb = upload_auxiliary_data_to_gracedb
		# None to disable periodic snapshots, otherwise seconds
		self.likelihood_snapshot_interval = likelihood_snapshot_interval
		self.likelihood_snapshot_timestamp = None
		# gracedb far threshold
		self.gracedb_far_threshold = gracedb_far_threshold
		self.gracedb_min_instruments = gracedb_min_instruments
		self.gracedb_group = gracedb_group
		self.gracedb_search = gracedb_search
		self.gracedb_pipeline = gracedb_pipeline
		self.gracedb_service_url = gracedb_service_url

		#
		# seglistdicts is populated the pipeline handler.
		#

		self.seglistdicts = None

		#
		# setup bottle routes
		#

		bottle.route("/latency_histogram.txt")(self.web_get_latency_histogram)
		bottle.route("/latency_history.txt")(self.web_get_latency_history)
		bottle.route("/snr_history.txt")(self.web_get_snr_history)
		for instrument in rankingstat.instruments:
			bottle.route("/%s_snr_history.txt" % instrument)(lambda : self.web_get_sngl_snr_history(ifo = instrument))
		bottle.route("/likelihood_history.txt")(self.web_get_likelihood_history)
		bottle.route("/far_history.txt")(self.web_get_far_history)
		bottle.route("/ram_history.txt")(self.web_get_ram_history)
		bottle.route("/rankingstat.xml")(self.web_get_rankingstat)
		bottle.route("/zerolag_rankingstatpdf.xml")(self.web_get_zerolag_rankingstatpdf)
		bottle.route("/gracedb_far_threshold.txt", method = "GET")(self.web_get_gracedb_far_threshold)
		bottle.route("/gracedb_far_threshold.txt", method = "POST")(self.web_set_gracedb_far_threshold)
		bottle.route("/gracedb_min_instruments.txt", method = "GET")(self.web_get_gracedb_min_instruments)
		bottle.route("/gracedb_min_instruments.txt", method = "POST")(self.web_set_gracedb_min_instruments)
		bottle.route("/sngls_snr_threshold.txt", method = "GET")(self.web_get_sngls_snr_threshold)
		bottle.route("/sngls_snr_threshold.txt", method = "POST")(self.web_set_sngls_snr_threshold)

		#
		# all triggers up to this index have had their SNR time
		# series deleted to save memory
		#

		self.snr_time_series_cleanup_index = 0

		#
		# attach a StreamThinca instance to ourselves
		#

		self.stream_thinca = streamthinca.StreamThinca(
			coincidence_threshold = rankingstat.delta_t,
			thinca_interval = thinca_interval,	# seconds
			min_instruments = rankingstat.min_instruments,
			min_log_L = min_log_L,
			sngls_snr_threshold = sngls_snr_threshold
		)

		#
		# setup likelihood ratio book-keeping.
		#
		# in online mode, if ranking_stat_input_url is set then on
		# each snapshot interval, and before providing stream
		# thinca with its ranking statistic information, the
		# current rankingstat object is replaced with the contents
		# of that file.  this is intended to be used by trigger
		# generator jobs on the injection branch of an online
		# analysis to import ranking statistic information from
		# their non-injection cousins instead of using whatever
		# statistics they've collected internally.
		# ranking_stat_input_url is not used when running offline.
		#
		# ranking_stat_output_url provides the name of the file to
		# which the internally-collected ranking statistic
		# information is to be written whenever output is written
		# to disk.  if set to None, then only the trigger file will
		# be written, no ranking statistic information will be
		# written.  normally it is set to a non-null value, but
		# injection jobs might be configured to disable ranking
		# statistic output since they produce nonsense.
		#

		self.ranking_stat_input_url = ranking_stat_input_url
		self.ranking_stat_output_url = ranking_stat_output_url
		self.rankingstat = rankingstat

		#
		# if we have been supplied with external ranking statistic
		# information then use it to enable ranking statistic
		# assignment in streamthinca.  otherwise, if we have not
		# been and yet we have been asked to apply the min log L
		# cut anyway then enable ranking statistic assignment using
		# the dataless ranking statistic variant
		#

		if self.ranking_stat_input_url is not None:
			self.stream_thinca.rankingstat = far.OnlineFrakensteinRankingStat(self.rankingstat, self.rankingstat).finish()
		elif min_log_L is not None:
			self.stream_thinca.rankingstat = far.DatalessRankingStat(
				template_ids = rankingstat.template_ids,
				instruments = rankingstat.instruments,
				min_instruments = rankingstat.min_instruments,
				delta_t = rankingstat.delta_t
			).finish()

		#
		# zero_lag_ranking_stats is a RankingStatPDF object that is
		# used to accumulate a histogram of the likelihood ratio
		# values assigned to zero-lag candidates.  this is required
		# to implement the extinction model for low-significance
		# events during online running but otherwise is optional.
		#
		# FIXME:  if the file does not exist or is not readable,
		# the code silently initializes a new, empty, histogram.
		# it would be better to determine whether or not the file
		# is required and fail when it is missing
		#

		if zerolag_rankingstatpdf_url is not None and os.access(ligolw_utils.local_path_from_url(zerolag_rankingstatpdf_url), os.R_OK):
			_, self.zerolag_rankingstatpdf = far.parse_likelihood_control_doc(ligolw_utils.load_url(zerolag_rankingstatpdf_url, verbose = verbose, contenthandler = far.RankingStat.LIGOLWContentHandler))
			if self.zerolag_rankingstatpdf is None:
				raise ValueError("\"%s\" does not contain ranking statistic PDF data" % zerolag_rankingstatpdf_url)
		elif zerolag_rankingstatpdf_url is not None:
			# initialize an all-zeros set of PDFs
			self.zerolag_rankingstatpdf = far.RankingStatPDF(rankingstat, nsamples = 0)
		else:
			self.zerolag_rankingstatpdf = None
		self.zerolag_rankingstatpdf_url = zerolag_rankingstatpdf_url

		#
		# rankingstatpdf contains the RankingStatPDF object (loaded
		# from rankingstatpdf_url) used to initialize the FAPFAR
		# object for on-the-fly FAP and FAR assignment.  except to
		# initialize the FAPFAR object it is not used for anything,
		# but is retained so that it can be exposed through the web
		# interface for diagnostic purposes and uploaded to gracedb
		# with candidates.  the extinction model is applied to
		# initialize the FAPFAR object but the original is retained
		# for upload to gracedb, etc.
		#

		self.rankingstatpdf_url = rankingstatpdf_url
		self.load_rankingstat_pdf()

		#
		# Fun output stuff
		#
		
		self.latency_histogram = rate.BinnedArray(rate.NDBins((rate.LinearPlusOverflowBins(5, 205, 22),)))
		self.latency_history = deque(maxlen = 1000)
		self.snr_history = deque(maxlen = 1000)
		self.likelihood_history = deque(maxlen = 1000)
		self.far_history = deque(maxlen = 1000)
		self.ram_history = deque(maxlen = 2)
		self.ifo_snr_history = dict((instrument, deque(maxlen = 10000)) for instrument in rankingstat.instruments)

	def load_rankingstat_pdf(self):
		# FIXME:  if the file can't be accessed the code silently
		# disables FAP/FAR assignment.  need to figure out when
		# failure is OK and when it's not OK and put a better check
		# here.
		if self.rankingstatpdf_url is not None and os.access(ligolw_utils.local_path_from_url(self.rankingstatpdf_url), os.R_OK):
			_, self.rankingstatpdf = far.parse_likelihood_control_doc(ligolw_utils.load_url(self.rankingstatpdf_url, verbose = self.verbose, contenthandler = far.RankingStat.LIGOLWContentHandler))
			if self.rankingstatpdf is None:
				raise ValueError("\"%s\" does not contain ranking statistic PDFs" % url)
			if not self.rankingstat.template_ids <= self.rankingstatpdf.template_ids:
				raise ValueError("\"%s\" is for the wrong templates")
			self.fapfar = far.FAPFAR(self.rankingstatpdf.new_with_extinction())
		else:
			self.rankingstatpdf = None
			self.fapfar = None

	def appsink_new_buffer(self, elem):
		with self.lock:
			# retrieve triggers from appsink element
			buf = elem.emit("pull-sample").get_buffer()
			events = []
			for i in range(buf.n_memory()):
				memory = buf.peek_memory(i)
				result, mapinfo = memory.map(Gst.MapFlags.READ)
				assert result
				# NOTE NOTE NOTE NOTE
				# It is critical that the correct class'
				# .from_buffer() method be used here.  This
				# code is interpreting the buffer's
				# contents as an array of C structures and
				# building instances of python wrappers of
				# those structures but if the python
				# wrappers are for the wrong structure
				# declaration then terrible terrible things
				# will happen
				# NOTE NOTE NOTE NOTE
				# FIXME why does mapinfo.data come out as
				# an empty list on some occasions???
				if mapinfo.data:
					events.extend(streamthinca.SnglInspiral.from_buffer(mapinfo.data))
				memory.unmap(mapinfo)

			# FIXME:  ugly way to get the instrument
			instrument = elem.get_name().split("_", 1)[0]

			# extract segment.  move the segment's upper
			# boundary to include all triggers.  ARGH the 1 ns
			# offset is needed for the respective trigger to be
			# "in" the segment (segments are open from above)
			# FIXME:  is there another way?
			buf_timestamp = LIGOTimeGPS(0, buf.pts)
			buf_seg = segments.segment(buf_timestamp, max((buf_timestamp + LIGOTimeGPS(0, buf.duration),) + tuple(event.end + 0.000000001 for event in events)))
			buf_is_gap = bool(buf.mini_object.flags & Gst.BufferFlags.GAP)
			# sanity check that gap buffers are empty
			assert not (buf_is_gap and events)

			# safety check end times.  OK for end times to be
			# past end of buffer, but we cannot allow triggr
			# times to go backwards.  they cannot precede the
			# buffer's start because, below,
			# streamthinca.add_events() will be told the
			# trigger list is complete upto this buffer's time
			# stamp.
			assert all(event.end >= buf_timestamp for event in events)
			# we have extended the buf_seg above to enclose the
			# triggers, make sure that worked
			assert all(event.end in buf_seg for event in events)

			# Find max SNR sngles
			if events:
				max_snr_event = max(events, key = lambda t: t.snr)
				self.ifo_snr_history[max_snr_event.ifo].append((float(max_snr_event.end), max_snr_event.snr))

			# set metadata on triggers.  because this uses the
			# ID generator attached to the database-backed
			# sngl_inspiral table, and that generator has been
			# synced to the database' contents, the IDs
			# assigned here will not collide with any already
			# in the database
			for event in events:
				event.process_id = self.coincs_document.process_id
				event.event_id = self.coincs_document.get_next_sngl_id()

			# update likelihood snapshot if needed
			if self.likelihood_snapshot_interval is not None and (self.likelihood_snapshot_timestamp is None or buf_timestamp - self.likelihood_snapshot_timestamp >= self.likelihood_snapshot_interval):
				self.likelihood_snapshot_timestamp = buf_timestamp

				# post a checkpoint message.
				# FIXME:  make sure this triggers
				# self.snapshot_output_url() to be invoked.
				# lloidparts takes care of that for now,
				# but spreading the program logic around
				# like that isn't a good idea, this code
				# should be responsible for it somehow, no?
				# NOTE: self.snapshot_output_url() writes
				# the current rankingstat object to the
				# location identified by .ranking_stat_output_url,
				# so if that is either not set or at least
				# set to a different name than
				# .ranking_stat_input_url the file that has
				# just been loaded above will not be
				# overwritten.
				self.pipeline.get_bus().post(message_new_checkpoint(self.pipeline, timestamp = buf_timestamp.ns()))

				# if a ranking statistic source url is set
				# and is not the same as the file to which
				# we are writing our ranking statistic data
				# then overwrite rankingstat with its
				# contents.  the use case is online
				# injection jobs that need to periodically
				# grab new ranking statistic data from
				# their corresponding non-injection partner
				if self.ranking_stat_input_url is not None and self.ranking_stat_input_url != self.ranking_stat_output_url:
					params_before = self.rankingstat.template_ids, self.rankingstat.instruments, self.rankingstat.min_instruments, self.rankingstat.delta_t
					self.rankingstat, _ = far.parse_likelihood_control_doc(ligolw_utils.load_url(self.ranking_stat_input_url, verbose = self.verbose, contenthandler = far.RankingStat.LIGOLWContentHandler))
					if params_before != (self.rankingstat.template_ids, self.rankingstat.instruments, self.rankingstat.min_instruments, self.rankingstat.delta_t):
						raise ValueError("'%s' contains incompatible ranking statistic configuration" % self.ranking_stat_input_url)

				# update streamthinca's ranking statistic
				# data
				self.stream_thinca.rankingstat = far.OnlineFrakensteinRankingStat(self.rankingstat, self.rankingstat).finish()

				# optionally get updated ranking statistic
				# PDF data and enable FAP/FAR assignment
				self.load_rankingstat_pdf()

			# add triggers to trigger rate record.  this needs
			# to be done without any cuts on coincidence, etc.,
			# so that the total trigger count agrees with the
			# total livetime from the SNR buffers.  we assume
			# the density of real signals is so small that this
			# count is not sensitive to their presence.  NOTE:
			# this is not true locally.  a genuine signal, if
			# loud, can significantly increase the local
			# density of triggers, therefore the trigger rate
			# measurement machinery must take care to average
			# the rate over a sufficiently long period of time
			# that the rate estimates are insensitive to the
			# presence of signals.  the current implementation
			# averages over whole science segments.  NOTE: this
			# must be done before running stream thinca (below)
			# so that the "how many instruments were on test"
			# is aware of this buffer.
			if not buf_is_gap:
				self.rankingstat.denominator.triggerrates[instrument].add_ratebin(map(float, buf_seg), len(events))

			# extract times when instruments were producing
			# SNR.  used to define "on instruments" for coinc
			# tables among other things.  will only need
			# segment information for the times for which we
			# have triggers, so use stream_thinca's discard
			# boundary and a bisection search to clip the lists
			# to reduce subsequent operation count.
			discard_boundary = float(self.stream_thinca.discard_boundary)
			snr_segments = segments.segmentlistdict((instrument, ratebinlist[lower_bound_in_seglist(ratebinlist, discard_boundary):].segmentlist()) for instrument, ratebinlist in self.rankingstat.denominator.triggerrates.items())

			# times when SNR was available.  used only for code
			# correctness checks
			one_or_more_instruments = segmentsUtils.vote(snr_segments.values(), 1)
			# FIXME:  this is needed to work around rounding
			# problems in safety checks below, trying to
			# compare GPS trigger times to float segment
			# boundaries (the boundaries don't have enough
			# precision to know if triggers near the edge are
			# in or out).  it would be better not to have to
			# screw around like this.
			one_or_more_instruments.protract(1e-3)	# 1 ms

			# times when at least 2 instruments were generating
			# SNR.  used to sieve triggers for inclusion in the
			# denominator.
			two_or_more_instruments = segmentsUtils.vote(snr_segments.values(), 2)
			# FIXME:  see comment above.
			two_or_more_instruments.protract(1e-3)	# 1 ms

			# run stream thinca.  update the parameter
			# distribution data from sngls that weren't used in
			# zero-lag multi-instrument coincs.  NOTE:  we rely
			# on the arguments to .chain() being evaluated in
			# left-to-right order so that .add_events() is
			# evaluated before .last_coincs because the former
			# initializes the latter.  we skip singles
			# collected during times when only one instrument
			# was on.  NOTE:  the definition of "on" is blurry
			# since we can recover triggers with end times
			# outside of the available strain data, but the
			# purpose of this test is simply to prevent signals
			# occuring during single-detector times from
			# contaminating our noise model, so it's not
			# necessary for this test to be super precisely
			# defined.
			for event in itertools.chain(self.stream_thinca.add_events(self.coincs_document.xmldoc, self.coincs_document.process_id, events, buf_timestamp, snr_segments, fapfar = self.fapfar), self.stream_thinca.last_coincs.single_sngl_inspirals() if self.stream_thinca.last_coincs else ()):
				if self.ranking_stat_output_url is None:
					continue
				assert event.end in one_or_more_instruments, "trigger at time (%s) with no SNR (%s)" % (str(event.end), str(one_or_more_instruments))
				if event.end in two_or_more_instruments:
					self.rankingstat.denominator.increment(event)
			self.coincs_document.commit()

			# update zero-lag bin counts in rankingstat.
			if self.stream_thinca.last_coincs and self.ranking_stat_output_url is not None:
				for coinc_event_id, coinc_event in self.stream_thinca.last_coincs.coinc_event_index.items():
					if coinc_event.time_slide_id in self.stream_thinca.last_coincs.zero_lag_time_slide_ids:
						for event in self.stream_thinca.last_coincs.sngl_inspirals(coinc_event_id):
							self.rankingstat.zerolag.increment(event)

			# Cluster last coincs before recording number of zero
			# lag events or sending alerts to gracedb
			# FIXME Do proper clustering that saves states between
			# thinca intervals and uses an independent clustering
			# window. This can also go wrong if there are multiple
			# events with an identical likelihood.  It will just
			# choose the event with the highest event id
			if self.stream_thinca.last_coincs:
				self.stream_thinca.last_coincs.coinc_event_index = dict([max(self.stream_thinca.last_coincs.coinc_event_index.items(), key = lambda (coinc_event_id, coinc_event): coinc_event.likelihood)])

			# Add events to the observed likelihood histogram
			# post "clustering"
			# FIXME proper clustering is really needed (see
			# above)
			if self.stream_thinca.last_coincs and self.zerolag_rankingstatpdf is not None:
				for coinc_event_id, coinc_event in self.stream_thinca.last_coincs.coinc_event_index.items():
					if coinc_event.likelihood is not None and coinc_event.time_slide_id in self.stream_thinca.last_coincs.zero_lag_time_slide_ids:
						self.zerolag_rankingstatpdf.zero_lag_lr_lnpdf.count[coinc_event.likelihood,] += 1

			# do GraceDB alerts
			if self.gracedb_far_threshold is not None:
				self.__do_gracedb_alerts()
				self.__update_eye_candy()

			# after doing alerts, no longer need per-trigger
			# SNR data for the triggers that are too old to be
			# used to form candidates.  to avoid searching the
			# entire list of triggers each time, we stop when
			# we encounter the first trigger whose SNR series
			# might still be needed, save its index, and start
			# the search from there next time
			# FIXME:  could also trim segment and V data from
			# ranking stat object if ranking_stat_output_url is
			# not set because the info won't be used
			discard_boundary = self.stream_thinca.discard_boundary
			for self.snr_time_series_cleanup_index, event in enumerate(self.coincs_document.sngl_inspiral_table[self.snr_time_series_cleanup_index:], self.snr_time_series_cleanup_index):
				if event.end >= discard_boundary:
					break
				del event.snr_time_series

	def T050017_filename(self, description, extension):
		segs = segments.segmentlist(seglistdict.extent_all() for seglistdict in self.seglistdicts.values() if any(seglistdict.values()))
		if segs:
			start, end = segs.extent()
		else:
			# silence errors at start-up.
			# FIXME:  this is probably dumb.  who cares.
			start = end = now()
		start, end = int(math.floor(start)), int(math.ceil(end))
		return "%s-%s-%d-%d.%s" % ("".join(sorted(self.rankingstat.instruments)), description, start, end - start, extension)

	def __get_rankingstat_xmldoc(self):
		# generate a coinc parameter distribution document.  NOTE:
		# likelihood ratio PDFs *are* included if they were present in
		# the --likelihood-file that was loaded.
		xmldoc = ligolw.Document()
		xmldoc.appendChild(ligolw.LIGO_LW())
		process = ligolw_process.register_to_xmldoc(xmldoc, u"gstlal_inspiral", paramdict = {}, ifos = self.rankingstat.instruments)
		far.gen_likelihood_control_doc(xmldoc, self.rankingstat, self.rankingstatpdf)
		ligolw_process.set_process_end_time(process)
		return xmldoc

	def web_get_rankingstat(self):
		with self.lock:
			output = StringIO.StringIO()
			ligolw_utils.write_fileobj(self.__get_rankingstat_xmldoc(), output)
			outstr = output.getvalue()
			output.close()
			return outstr

	def __get_zerolag_rankingstatpdf_xmldoc(self):
		xmldoc = ligolw.Document()
		xmldoc.appendChild(ligolw.LIGO_LW())
		process = ligolw_process.register_to_xmldoc(xmldoc, u"gstlal_inspiral", paramdict = {}, ifos = self.rankingstat.instruments)
		far.gen_likelihood_control_doc(xmldoc, None, self.zerolag_rankingstatpdf)
		ligolw_process.set_process_end_time(process)
		return xmldoc

	def web_get_zerolag_rankingstatpdf(self):
		with self.lock:
			output = StringIO.StringIO()
			ligolw_utils.write_fileobj(self.__get_zerolag_rankingstatpdf_xmldoc(), output)
			outstr = output.getvalue()
			output.close()
			return outstr

	def __flush(self):
		# run StreamThinca's .flush().  returns the last remaining
		# non-coincident sngls.  add them to the distribution.  as
		# above in appsink_new_buffer() we skip singles collected
		# during times when only one instrument was one.

		# times when at least 2 instruments were generating SNR.
		# used to sieve triggers for inclusion in the denominator.
		discard_boundary = float(self.stream_thinca.discard_boundary)
		snr_segments = segments.segmentlistdict((instrument, ratebinlist[lower_bound_in_seglist(ratebinlist, discard_boundary):].segmentlist()) for instrument, ratebinlist in self.rankingstat.denominator.triggerrates.items())
		one_or_more_instruments = segmentsUtils.vote(snr_segments.values(), 1)
		# FIXME:  see comment in appsink_new_buffer()
		one_or_more_instruments.protract(1e-3)	# 1 ms
		two_or_more_instruments = segmentsUtils.vote(snr_segments.values(), 2)
		# FIXME:  see comment in appsink_new_buffer()
		two_or_more_instruments.protract(1e-3)	# 1 ms

		ratebinlists = self.rankingstat.denominator.triggerrates.values()
		for event in self.stream_thinca.flush(self.coincs_document.xmldoc, self.coincs_document.process_id, snr_segments, fapfar = self.fapfar):
			if self.ranking_stat_output_url is None:
				continue
			assert event.end in one_or_more_instruments, "trigger at time (%s) with no SNR (%s)" % (str(event.end), str(one_or_more_instruments))
			if event.end in two_or_more_instruments:
				self.rankingstat.denominator.increment(event)
		self.coincs_document.commit()

		# update zero-lag bin counts in rankingstat.
		if self.stream_thinca.last_coincs and self.ranking_stat_output_url is not None:
			for coinc_event_id, coinc_event in self.stream_thinca.last_coincs.coinc_event_index.items():
				if coinc_event.time_slide_id in self.stream_thinca.last_coincs.zero_lag_time_slide_ids:
					for event in self.stream_thinca.last_coincs.sngl_inspirals(coinc_event_id):
						self.rankingstat.zerolag.increment(event)

		# Cluster last coincs before recording number of zero
		# lag events or sending alerts to gracedb
		# FIXME Do proper clustering that saves states between
		# thinca intervals and uses an independent clustering
		# window. This can also go wrong if there are multiple
		# events with an identical likelihood.  It will just
		# choose the event with the highest event id
		if self.stream_thinca.last_coincs:
			self.stream_thinca.last_coincs.coinc_event_index = dict([max(self.stream_thinca.last_coincs.coinc_event_index.items(), key = lambda (coinc_event_id, coinc_event): coinc_event.likelihood)])

		# Add events to the observed likelihood histogram post
		# "clustering"
		# FIXME proper clustering is really needed (see above)
		if self.stream_thinca.last_coincs and self.zerolag_rankingstatpdf is not None:
			for coinc_event_id, coinc_event in self.stream_thinca.last_coincs.coinc_event_index.items():
				if coinc_event.likelihood is not None and coinc_event.time_slide_id in self.stream_thinca.last_coincs.zero_lag_time_slide_ids:
					self.zerolag_rankingstatpdf.zero_lag_lr_lnpdf.count[coinc_event.likelihood,] += 1

		# do GraceDB alerts
		if self.gracedb_far_threshold is not None:
			self.__do_gracedb_alerts()

		# after doing alerts, no longer need per-trigger SNR data
		# for the triggers that are too old to be used to form
		# candidates.  to avoid searching the entire list of
		# triggers each time, we stop when we encounter the first
		# trigger whose SNR series might still be needed, save its
		# index, and start the search from there next time
		discard_boundary = self.stream_thinca.discard_boundary
		for self.snr_time_series_cleanup_index, event in enumerate(self.coincs_document.sngl_inspiral_table[self.snr_time_series_cleanup_index:], self.snr_time_series_cleanup_index):
			if event.end >= discard_boundary:
				break
			del event.snr_time_series

		# garbage collect last_coincs
		# FIXME:  this should not be needed.  something is wrong.
		# if this is needed, then why don't we have to garbage
		# collect everything ourselves?
		self.stream_thinca.last_coincs = {}

	def flush(self):
		with self.lock:
			self.__flush()

	def __do_gracedb_alerts(self, retries = 5, retry_delay = 5.):
		# sanity check
		if self.fapfar is None:
			raise ValueError("gracedb alerts cannot be enabled without fap/far data")

		# no-op short circuit
		if not self.stream_thinca.last_coincs:
			return

		gracedb_client = gracedb.rest.GraceDb(self.gracedb_service_url)
		gracedb_ids = []
		coinc_inspiral_index = self.stream_thinca.last_coincs.coinc_inspiral_index

		# This appears to be a silly for loop since
		# coinc_event_index will only have one value, but we're
		# future proofing this at the point where it could have
		# multiple clustered events
		for coinc_event in self.stream_thinca.last_coincs.coinc_event_index.values():
			#
			# continue if the false alarm rate is not low
			# enough, or is nan, or there aren't enough
			# instruments participating in this coinc
			#

			if coinc_inspiral_index[coinc_event.coinc_event_id].combined_far is None or coinc_inspiral_index[coinc_event.coinc_event_id].combined_far > self.gracedb_far_threshold or numpy.isnan(coinc_inspiral_index[coinc_event.coinc_event_id].combined_far) or len(self.stream_thinca.last_coincs.sngl_inspirals(coinc_event.coinc_event_id)) < self.gracedb_min_instruments:
				continue

			#
			# fake a filename for end-user convenience
			#

			observatories = "".join(sorted(set(instrument[0] for instrument in self.rankingstat.instruments)))
			instruments = "".join(sorted(self.rankingstat.instruments))
			description = "%s_%s_%s_%s" % (instruments, ("%.4g" % coinc_inspiral_index[coinc_event.coinc_event_id].mass).replace(".", "_").replace("-", "_"), self.gracedb_group, self.gracedb_search)
			end_time = int(coinc_inspiral_index[coinc_event.coinc_event_id].end)
			filename = "%s-%s-%d-%d.xml" % (observatories, description, end_time, 0)

			#
			# construct message and send to gracedb.
			# we go through the intermediate step of
			# first writing the document into a string
			# buffer incase there is some safety in
			# doing so in the event of a malformed
			# document;  instead of writing directly
			# into gracedb's input pipe and crashing
			# part way through.
			#

			if self.verbose:
				print >>sys.stderr, "sending %s to gracedb ..." % filename
			message = StringIO.StringIO()
			xmldoc = self.stream_thinca.last_coincs[coinc_event.coinc_event_id]
			# give the alert all the standard inspiral
			# columns (attributes should all be
			# populated).  FIXME:  ugly.
			sngl_inspiral_table = lsctables.SnglInspiralTable.get_table(xmldoc)
			for standard_column in ("process_id", "ifo", "search", "channel", "end_time", "end_time_ns", "end_time_gmst", "impulse_time", "impulse_time_ns", "template_duration", "event_duration", "amplitude", "eff_distance", "coa_phase", "mass1", "mass2", "mchirp", "mtotal", "eta", "kappa", "chi", "tau0", "tau2", "tau3", "tau4", "tau5", "ttotal", "psi0", "psi3", "alpha", "alpha1", "alpha2", "alpha3", "alpha4", "alpha5", "alpha6", "beta", "f_final", "snr", "chisq", "chisq_dof", "bank_chisq", "bank_chisq_dof", "cont_chisq", "cont_chisq_dof", "sigmasq", "rsqveto_duration", "Gamma0", "Gamma1", "Gamma2", "Gamma3", "Gamma4", "Gamma5", "Gamma6", "Gamma7", "Gamma8", "Gamma9", "spin1x", "spin1y", "spin1z", "spin2x", "spin2y", "spin2z", "event_id"):
				try:
					sngl_inspiral_table.appendColumn(standard_column)
				except ValueError:
					# already has it
					pass
			# add SNR time series if available
			for event in self.stream_thinca.last_coincs.sngl_inspirals(coinc_event.coinc_event_id):
				snr_time_series = event.snr_time_series
				if snr_time_series is not None:
					xmldoc.childNodes[-1].appendChild(lalseries.build_COMPLEX8TimeSeries(snr_time_series)).appendChild(ligolw_param.Param.from_pyvalue(u"event_id", event.event_id))
			# serialize to XML
			ligolw_utils.write_fileobj(xmldoc, message, gz = False)
			xmldoc.unlink()
			# FIXME: make this optional from command line?
			if True:
				for attempt in range(1, retries + 1):
					try:
						resp = gracedb_client.createEvent(self.gracedb_group, self.gracedb_pipeline, filename, filecontents = message.getvalue(), search = self.gracedb_search)
					except gracedb.rest.HTTPError as resp:
						pass
					else:
						resp_json = resp.json()
						if resp.status == httplib.CREATED:
							if self.verbose:
								print >>sys.stderr, "event assigned grace ID %s" % resp_json["graceid"]
							gracedb_ids.append(resp_json["graceid"])
							break
					print >>sys.stderr, "gracedb upload of %s failed on attempt %d/%d: %d: %s"  % (filename, attempt, retries, resp.status, httplib.responses.get(resp.status, "Unknown"))
					time.sleep(random.lognormal(math.log(retry_delay), .5))
				else:
					print >>sys.stderr, "gracedb upload of %s failed" % filename
			else:
				proc = subprocess.Popen(("/bin/cp", "/dev/stdin", filename), stdin = subprocess.PIPE)
				proc.stdin.write(message.getvalue())
				proc.stdin.flush()
				proc.stdin.close()
			message.close()

		if gracedb_ids and self.upload_auxiliary_data_to_gracedb:
			#
			# retrieve and upload PSDs
			#

			if self.verbose:
				print >>sys.stderr, "retrieving PSDs from whiteners and generating psd.xml.gz ..."
			psddict = {}
			for instrument in self.rankingstat.instruments:
				elem = self.pipeline.get_by_name("lal_whiten_%s" % instrument)
				data = numpy.array(elem.get_property("mean-psd"))
				psddict[instrument] = lal.CreateREAL8FrequencySeries(
					name = "PSD",
					epoch = LIGOTimeGPS(lal.UTCToGPS(time.gmtime()), 0),
					f0 = 0.0,
					deltaF = elem.get_property("delta-f"),
					sampleUnits = lal.Unit("s strain^2"),	# FIXME:  don't hard-code this
					length = len(data)
				)
				psddict[instrument].data.data = data
			fobj = StringIO.StringIO()
			reference_psd.write_psd_fileobj(fobj, psddict, gz = True)
			message, filename, tag, contents = ("strain spectral densities", "psd.xml.gz", "psd", fobj.getvalue())
			self.__upload_gracedb_aux_data(message, filename, tag, contents, gracedb_ids, retries, gracedb_client)

			#
			# retrieve and upload Ranking Data
			#

			if self.verbose:
				print >>sys.stderr, "generating ranking_data.xml.gz ..."
			fobj = StringIO.StringIO()
			ligolw_utils.write_fileobj(self.__get_rankingstat_xmldoc(), fobj, gz = True)
			message, filename, tag, contents = ("ranking statistic PDFs", "ranking_data.xml.gz", "ranking statistic", fobj.getvalue())
			del fobj
			self.__upload_gracedb_aux_data(message, filename, tag, contents, gracedb_ids, retries, gracedb_client)

	def __upload_gracedb_aux_data(self, message, filename, tag, contents, gracedb_ids, retries, gracedb_client, retry_delay = 5):
		for gracedb_id in gracedb_ids:
			for attempt in range(1, retries + 1):
				try:
					resp = gracedb_client.writeLog(gracedb_id, message, filename = filename, filecontents = contents, tagname = tag)
				except gracedb.rest.HTTPError as resp:
					pass
				else:
					if resp.status == httplib.CREATED:
						break
				print >>sys.stderr, "gracedb upload of %s for ID %s failed on attempt %d/%d: %d: %s"  % (filename, gracedb_id, attempt, retries, resp.status, httplib.responses.get(resp.status, "Unknown"))
				time.sleep(random.lognormal(math.log(retry_delay), .5))
			else:
				print >>sys.stderr, "gracedb upload of %s for ID %s failed" % (filename, gracedb_id)

	def do_gracedb_alerts(self):
		with self.lock:
			self.__do_gracedb_alerts()

	def __update_eye_candy(self):
		self.ram_history.append((float(lal.UTCToGPS(time.gmtime())), (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss + resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1048576.)) # GB
		if self.stream_thinca.last_coincs:
			latency_val = None
			snr_val = (0,0)
			like_val = (0,0)
			far_val = (0,0)
			coinc_inspiral_index = self.stream_thinca.last_coincs.coinc_inspiral_index
			coinc_event_index = self.stream_thinca.last_coincs.coinc_event_index
			for coinc_event_id, coinc_inspiral in coinc_inspiral_index.items():
				# FIXME:  update when a proper column is available
				latency = coinc_inspiral.minimum_duration
				self.latency_histogram[latency,] += 1
				if latency_val is None:
					t = float(coinc_inspiral_index[coinc_event_id].end)
					latency_val = (t, latency)
				snr = coinc_inspiral_index[coinc_event_id].snr
				if snr >= snr_val[1]:
					t = float(coinc_inspiral_index[coinc_event_id].end)
					snr_val = (t, snr)
			for coinc_event_id, coinc_inspiral in coinc_event_index.items():
				like = coinc_event_index[coinc_event_id].likelihood
				if like >= like_val[1]:
					t = float(coinc_inspiral_index[coinc_event_id].end)
					like_val = (t, like)
					far_val = (t, coinc_inspiral_index[coinc_event_id].combined_far)
			if latency_val is not None:
				self.latency_history.append(latency_val)
			if snr_val != (0,0):
				self.snr_history.append(snr_val)
			if like_val != (0,0):
				self.likelihood_history.append(like_val)
			if far_val != (0,0):
				self.far_history.append(far_val)

	def update_eye_candy(self):
		with self.lock:
			self.__update_eye_candy()

	def web_get_latency_histogram(self):
		with self.lock:
			for latency, number in zip(self.latency_histogram.centres()[0][1:-1], self.latency_histogram.array[1:-1]):
				yield "%e %e\n" % (latency, number)

	def web_get_latency_history(self):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, latency in self.latency_history:
				yield "%f %e\n" % (time, latency)

	def web_get_snr_history(self):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, snr in self.snr_history:
				yield "%f %e\n" % (time, snr)

	def web_get_sngl_snr_history(self, ifo):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, snr in self.ifo_snr_history[ifo]:
				yield "%f %e\n" % (time, snr)

	def web_get_likelihood_history(self):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, like in self.likelihood_history:
				yield "%f %e\n" % (time, like)

	def web_get_far_history(self):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, far in self.far_history:
				yield "%f %e\n" % (time, far)

	def web_get_ram_history(self):
		with self.lock:
			# first one in the list is sacrificed for a time stamp
			for time, ram in self.ram_history:
				yield "%f %e\n" % (time, ram)

	def web_get_gracedb_far_threshold(self):
		with self.lock:
			if self.gracedb_far_threshold is not None:
				yield "rate=%.17g\n" % self.gracedb_far_threshold
			else:
				yield "rate=\n"

	def web_set_gracedb_far_threshold(self):
		try:
			with self.lock:
				rate = bottle.request.forms["rate"]
				if rate:
					self.gracedb_far_threshold = float(rate)
					yield "OK: rate=%.17g\n" % self.gracedb_far_threshold
				else:
					self.gracedb_far_threshold = None
					yield "OK: rate=\n"
		except:
			yield "error\n"

	def web_get_gracedb_min_instruments(self):
		with self.lock:
			if self.gracedb_min_instruments is not None:
				yield "gracedb_min_instruments=%d\n" % self.gracedb_min_instruments
			else:
				yield "gracedb_min_instruments=\n"

	def web_set_gracedb_min_instruments(self):
		try:
			with self.lock:
				gracedb_min_instruments = bottle.request.forms["gracedb_min_instruments"]
				if gracedb_min_instruments is not None:
					self.gracedb_min_instruments = int(gracedb_min_instruments)
					yield "OK: gracedb_min_instruments=%d\n" % self.gracedb_min_instruments
				else:
					self.gracedb_min_instruments = None
					yield "OK: gracedb_min_instruments=\n"
		except:
			yield "error\n"

	def web_get_sngls_snr_threshold(self):
		with self.lock:
			if self.stream_thinca.sngls_snr_threshold is not None:
				yield "snr=%.17g\n" % self.stream_thinca.sngls_snr_threshold
			else:
				yield "snr=\n"

	def web_set_sngls_snr_threshold(self):
		try:
			with self.lock:
				snr_threshold = bottle.request.forms["snr"]
				if snr_threshold:
					self.stream_thinca.sngls_snr_threshold = float(snr_threshold)
					yield "OK: snr=%.17g\n" % self.stream_thinca.sngls_snr_threshold
				else:
					self.stream_thinca.sngls_snr_threshold = None
					yield "OK: snr=\n"
		except:
			yield "error\n"

	def __write_output_url(self, url = None, verbose = False):
		self.__flush()
		if url is not None:
			self.coincs_document.url = url
		self.coincs_document.write_output_url(seglistdicts = self.seglistdicts, verbose = verbose)
		# can't be used anymore
		del self.coincs_document

	def __write_ranking_stat_url(self, url, description, snapshot = False, verbose = False):
		# write the ranking statistic file.
		ligolw_utils.write_url(self.__get_rankingstat_xmldoc(), ligolw_utils.local_path_from_url(url), gz = (url or "stdout").endswith(".gz"), verbose = verbose, trap_signals = None)
		# Snapshots get their own custom file and path
		if snapshot:
			fname = self.T050017_filename(description + '_DISTSTATS', 'xml.gz')
			shutil.copy(ligolw_utils.local_path_from_url(url), os.path.join(subdir_from_T050017_filename(fname), fname))

	def __write_zero_lag_ranking_stat_url(self, url, verbose = False):
		ligolw_utils.write_url(self.__get_zerolag_rankingstatpdf_xmldoc(), url, gz = (url or "stdout").endswith(".gz"), verbose = verbose, trap_signals = None)

	def write_output_url(self, url = None, description = "", verbose = False):
		with self.lock:
			self.__write_output_url(url = url, verbose = verbose)
			if self.ranking_stat_output_url is not None:
				self.__write_ranking_stat_url(self.ranking_stat_output_url, description, verbose = verbose)

	def snapshot_output_url(self, description, extension, verbose = False):
		with self.lock:
			coincs_document = self.coincs_document.get_another()
			# We require the likelihood file to have the same name
			# as the input to this program to accumulate statistics
			# as we go
			fname = self.T050017_filename(description, extension)
			fname = os.path.join(subdir_from_T050017_filename(fname), fname)
			self.__write_output_url(url = fname, verbose = verbose)
			if self.ranking_stat_output_url is not None:
				self.__write_ranking_stat_url(self.ranking_stat_output_url, description, snapshot = True, verbose = verbose)
			if self.zerolag_rankingstatpdf is not None:
				self.__write_zero_lag_ranking_stat_url(self.zerolag_rankingstatpdf_url, verbose = verbose)
			self.coincs_document = coincs_document
			self.snr_time_series_cleanup_index = 0
