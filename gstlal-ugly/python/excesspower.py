#!/usr/bin/python

import sys

import numpy

from pylal import lalburst
from pylal.lalfft import XLALCreateForwardREAL8FFTPlan, XLALCreateReverseREAL8FFTPlan, XLALREAL8FreqTimeFFT
from pylal.xlal.datatypes.real8frequencyseries import REAL8FrequencySeries
from pylal.xlal.datatypes.complex16frequencyseries import COMPLEX16FrequencySeries
from pylal.xlal.datatypes.real8timeseries import REAL8TimeSeries
from pylal.xlal.window import XLALCreateHannREAL8Window

from glue.ligolw import ligolw
from glue.ligolw import ilwd
from glue.ligolw import utils
from glue.ligolw import lsctables

def build_filter(psd, rate=4096, flow=64, fhigh=2000, filter_len=0, b_wind=16.0, overlap=0.5, corr=None):
	"""Build a set of individual channel Hann window frequency filters (with bandwidth 'band') and then transfer them into the time domain as a matrix. The nth row of the matrix contains the time-domain filter for the flow+n*band*overlap frequency channel. The overlap is the fraction of the channel which overlaps with the previous channel. If filter_len is not set, then it defaults to nominal minimum width needed for the bandwidth requested."""

	if fhigh > rate/2:
		print >> sys.stderr, "WARNING: high frequency requested is higher than sampling rate / 2, adjusting to match."
		fhigh = rate/2

	if fhigh >= rate/2:
		print >> sys.stderr, "WARNING: high frequency is equal to Nyquist. Filters will probably be bad. Reduce the high frequency."

	# Filter length needs to be long enough to get the pertinent features in
	# the time domain
	filter_len = 2*int(2*b_wind/psd.deltaF)
	
	# define number of band window
	bands = int( (fhigh - flow) / b_wind / overlap ) - 1

	# Build spectral correlation function
	# NOTE: The default behavior is relative to the Hann window used in the
	# filter bank and NOT the whitener. It's just not right. Fair warning.
	# TODO: Is this default even needed anymore?
	if( corr == None ):
		wfftplan = XLALCreateForwardREAL8FFTPlan( filter_len, 1 )
		spec_corr = lalburst.XLALREAL8WindowTwoPointSpectralCorrelation(
			XLALCreateHannREAL8Window( filter_len ),
			wfftplan 
		)
	else:
		spec_corr = numpy.array( corr )

	# If no PSD is provided, set it equal to unity for all bins
	#if psd == None:
		#ifftplan = XLALCreateReverseREAL8FFTPlan( filter_len, 1 )
	#else:
	ifftplan = XLALCreateReverseREAL8FFTPlan( (len(psd.data)-1)*2, 1 )
	d_len = (len(psd.data)-1)*2

	filters = numpy.array([])
	for band in range( bands ):

		# Create the EP filter in the FD
		h_wind = lalburst.XLALCreateExcessPowerFilter( 
			#channel_flow =
			flow + band*b_wind*overlap,
			#channel_width =
			b_wind, 
			#psd =
			psd, 
			#correlation =
			spec_corr 
		)

		# Zero pad up to lowest frequency
		h_wind.data = numpy.hstack((numpy.zeros((int(h_wind.f0 / h_wind.deltaF), ), dtype = "complex"), h_wind.data))
		h_wind.f0 = 0.0
		d = h_wind.data
		# Zero pad window to get up to Nyquist
		h_wind.data = numpy.hstack((d, numpy.zeros((len(psd.data) - len(d),), dtype = "complex")))

		# DEBUG: Uncomment to dump FD filters
		#f = open( "filters_fd/hann_%dhz" % int( flow + band*b_wind*overlap ), "w" )
		#for freq, s in enumerate( h_wind.data ):
			#f.write( "%f %f\n" % (freq*h_wind.deltaF,s) )
		#f.close()

		# IFFT the window into a time series for use as a TD filter
		t_series = REAL8TimeSeries()
		t_series.data = numpy.zeros( (d_len,), dtype="double" ) 
		XLALREAL8FreqTimeFFT( 
			# t_series =
			t_series, 
			# window_freq_series =
			h_wind, 
			# ifft plan =
			ifftplan
		)

		td_filter = t_series.data
		td_filter = numpy.roll( td_filter, filter_len/2 )[:filter_len]
		## normalize the filters
		td_filter /= numpy.sqrt( sum( [ x**2 for x in td_filter ] ) )
		######################
		filters = numpy.concatenate( (filters, td_filter) )
		
		# DEBUG: Uncomment to dump TD filters
		#f = open( "filters_td/hann_%dhz" % int( flow + band*b_wind*overlap ), "w" )
		#for t, s in enumerate( td_filter ):
			#f.write( "%d %f\n" % (t,s) )
		#f.close()

	# Shape it into a "matrix-like" object
	filters.shape = ( bands, filter_len )
	return filters

def build_chan_matrix( nchannels=1, up_factor=0, norm=None ):
	"""Build the matrix to properly normalize nchannels coming out of the FIR filter. Norm should be an array of length equal to the number of output channels, with the proper normalization factor. up_factor controls the number of output channels. E.g. If thirty two input channels are indicated, and an up_factor of two is input, then an array of length eight corresponding to eight output channels are required. The output matrix uses 1/sqrt(A_i) where A_i is the element of the input norm array."""

	if( up_factor > int(numpy.log2(nchannels))+1 ):
		sys.exit( "up_factor cannot be larger than log2(nchannels)." )
	elif( up_factor < 0 ):
		sys.exit( "up_factor must be larger than or equal to 0." )

	# If no normalization coefficients are provided, default to unity
	if( norm == None ):
		norm = numpy.ones( nchannels >> up_factor )

	# Number of non-zero elements in that row
	n = 2**up_factor

	# Matrix row
	r0 = numpy.zeros(nchannels)
	m = []
	for i, mu_sq in enumerate(norm):
		r = r0.copy()
		if mu_sq > 0:
			r[i*n:(i+1)*n] = numpy.sqrt(1.0/mu_sq)
		else:  # End of the filter bank which we're killing
			r[i*n:(i+1)*n] = 0
		m.append( r )

	return numpy.array(m).T

def build_inner_product_norm( corr, band, del_f, nfilts, flow, psd=None, level=None, max_level=None ):
	"""Determine the mu^2(f_low, n*b) for higher bandiwdth channels from the base band. Returns a list where the indexes correspond to the 'upsample' factor - 1. For example, For 16 channels, An array of length 4 will be returned with the first index corresponding to 8 channels, second to 4, third to 2, and fourth to the single wide band channel. If level != None, then the specified index will be calculated and returned."""
	# TODO: can we build middle channels from one level down?

	# Recreate the Hann filter in the FD
	total_len = flow/del_f + nfilts*band/del_f  # Hardcoded to 50 % overlap
	total_len += 5*band/del_f # buffer at the end, shouldn't be needed

	wind_len = int(band*2/del_f)
	# Build the actual filter in the FD
	h_wind = XLALCreateHannREAL8Window( wind_len )
	d = h_wind.data
	d = numpy.hstack(
		(numpy.zeros((int(flow / del_f)), dtype = "complex"), d)
	)
	d = numpy.hstack((d, numpy.zeros((total_len - len(d),), dtype = "complex")))
	d = numpy.roll( d, -wind_len/4 )

	# Set of two filters to do the integral
	filter1 = COMPLEX16FrequencySeries()
	filter1.deltaF = del_f
	filter1.f0 = 0
	filter2 = COMPLEX16FrequencySeries()
	filter2.deltaF = del_f
	filter2.f0 = 0

	corr = numpy.array(corr)

	inner = []
	n, itr = 1, 0
	itr = 0
	max_level = min( max_level, numpy.ceil(numpy.log2(nfilts)) )
	while itr <= max_level:
		# Only one level was requested, skip until we find it
		#print "level %d" % itr
		if( level != None and level != itr ): continue
 
		foff = 0
		level_ar = []
		
		# The number of bands added (nb) is calculated just in case the 
		# number of filters at the edge is not equal to the normal number 
		# (n) at this resolution.
		nb = nfilts % n
		#mu_sq = n*band/del_f
		mu_sq = n
		for i in range( n-1 ):
			if( foff + i + 1 >= nfilts ):
				break # because we hit the end of the filter bank

			filter1.data = numpy.roll( d, (foff+i)*wind_len / 2 )
			filter2.data = numpy.roll( d, (foff+i+1)*wind_len / 2 )

			# TODO: fix when psd None vs NULL is sorted out
			if( psd == None ):
				mu_sq += lalburst.XLALExcessPowerFilterInnerProduct( 
					filter1, filter2, corr
				) * del_f/band * 2
			else:
				mu_sq += lalburst.XLALExcessPowerFilterInnerProduct( 
					filter1, filter2, corr, psd
				) * del_f/band * 2


			# DEBUG: Dump the FD filters
			#f = open( "filters_fd_corr/hann_%dhz" % int((foff+i*band)+flow), "w" )
			#for i, fdat in enumerate(filter1.data):
				#f.write( "%f %f\n" % (i*filter1.deltaF, fdat) )
			#f.close()

		# TODO: Since this is pretty much filter independent
		# drop the iteration and just multiply
		level_ar = numpy.ones( numpy.ceil( float(nfilts)/n ) )*mu_sq 

		# Filter at the end can be different than the others -- but we can't
		# undersample it properly, so kill it
		if( nb > 0 ):
			level_ar[-1] = 0

		# Only one level was requested, so return it.
		inner.append( numpy.array( level_ar ) )
		if( level == itr ): return inner[itr]

		# Move to the next higher channel bandwidth
		n *= 2
		itr += 1
		
	return inner

def build_fir_sq_adder( nsamp ):
	"""Just a square window of nsamp long. Used to sum samples in time."""
	return numpy.ones(nsamp)

def create_bank_xml(flow, fhigh, band, duration, detector=None):
	"""
	Create a bank of sngl_burst XML entries. This file is then used by the trigger generator to do trigger generation. Takes in the frequency parameters and filter duration and returns an ligolw entity with a sngl_burst Table which can be saved to a file.
	"""

	xmldoc = ligolw.Document()
	xmldoc.appendChild(ligolw.LIGO_LW())
	bank = lsctables.New(lsctables.SnglBurstTable,
	["peak_time_ns", "start_time_ns", "stop_time_ns",
	"process_id", "ifo", "peak_time", "start_time", "stop_time",
	"duration", "time_lag", "peak_frequency", "search",
	"central_freq", "channel", "amplitude", "snr", "confidence",
	"chisq", "chisq_dof",
	"flow", "fhigh", "bandwidth", "tfvolume", "hrss", "event_id"])
	bank.sync_next_id()

	cfreq = flow
	while cfreq < fhigh:
		row = bank.RowType()
		row.search = u"gstlal_excesspower"
		row.duration = duration
		row.bandwidth = 2*band
		row.peak_frequency = cfreq
		row.central_freq = cfreq
		row.flow = cfreq - band
		row.fhigh = cfreq + band
		row.ifo = detector

		# Stuff that doesn't matter
		row.peak_time_ns = 0
		row.peak_time = 0
		row.start_time_ns = 0
		row.start_time = 0
		row.stop_time_ns = 0
		row.stop_time = 0
		row.tfvolume = 0
		row.time_lag = 0
		row.amplitude = 0
		row.hrss = 0
		row.snr = 0
		row.chisq = 0
		row.chisq_dof = 0
		row.confidence = 0
		row.event_id = bank.get_next_id()
		row.channel = "awesome full of GW channel"
		# TODO: Probably should fix this entry.
		row.process_id = ilwd.get_ilwdchar( u"process:process_id:0" )

		bank.append( row )
		cfreq += band * 0.5 # overlap

	xmldoc.childNodes[0].appendChild(bank)
	return xmldoc

from glue import lal
from glue.segments import segment
import re

from scipy.stats import chi2
import numpy

def duration_from_cache( cachef ):
	cache = lal.Cache.fromfile( open( cachef ) )
	duration = cache[0].segment
	for entry in cache[1:]:
		duration |= entry.segment

	return duration[0], abs(duration)

def determine_thresh_from_fap( fap, ndof = 2 ):
	"""
	Given a false alarm probability desired, and a given number of degrees of freedom (ndof, default = 2), calculate the proper amplitude snr threshold for samples of tiles with that ndof. This is obtained by solving for the statistical value of a CDF for a chi_squared with ndof degrees of freedom at a given probability.
	"""

	return numpy.sqrt( chi2.ppf( 1-fap, ndof ) )

