# Copyright (C) 2017  Kipp Cannon
# Copyright (C) 2011--2014  Kipp Cannon, Chad Hanna, Drew Keppel
# Copyright (C) 2013  Jacob Peoples
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
# =============================================================================
#
#                                   Preamble
#
# =============================================================================
#


import math
import numpy
import random
from scipy import interpolate
from scipy import stats
import sys


from glue.ligolw import lsctables
from glue.ligolw import param as ligolw_param
from glue import segments
from gstlal.stats import horizonhistory
from gstlal.stats import inspiral_extrinsics
from gstlal.stats import trigger_rate
import lal
from lal import rate
from lalburst import snglcoinc
import lalsimulation


__all__ = [
	"LnSignalDensity",
	"LnNoiseDensity"
]


#
# =============================================================================
#
#                                  Base Class
#
# =============================================================================
#


class LnLRDensity(snglcoinc.LnLRDensity):
	# range of SNRs covered by this object
	snr_min = 4.

	# SNR, \chi^2 binning definition
	snr_chi_binning = rate.NDBins((rate.ATanLogarithmicBins(2.6, 26., 300), rate.ATanLogarithmicBins(.001, 0.2, 280)))

	def __init__(self, template_ids, instruments, delta_t, min_instruments = 2):
		#
		# check input
		#

		if template_ids is not None and not template_ids:
			raise ValueError("template_ids cannot be empty")
		if min_instruments < 1:
			raise ValueError("min_instruments=%d must be >= 1" % min_instruments)
		if min_instruments > len(instruments):
			raise ValueError("not enough instruments (%s) to satisfy min_instruments=%d" % (", ".join(sorted(instruments)), min_instruments))
		if delta_t < 0.:
			raise ValueError("delta_t=%g must be >= 0" % delta_t)

		#
		# initialize
		#

		self.template_ids = frozenset(template_ids) if template_ids is not None else template_ids
		self.instruments = frozenset(instruments)
		self.min_instruments = min_instruments
		self.delta_t = delta_t
		# install SNR, chi^2 PDFs.  numerator will override this
		self.densities = {}
		for instrument in instruments:
			self.densities["%s_snr_chi" % instrument] = rate.BinnedLnPDF(self.snr_chi_binning)

	def __iadd__(self, other):
		if type(other) != type(self):
			raise TypeError(other)
		# template_id set mismatch is allowed in the special case
		# that one or the other is None to make it possible to
		# construct generic seed objects providing initialization
		# data for the ranking statistics.
		if self.template_ids is not None and other.template_ids is not None and self.template_ids != other.template_ids:
			raise ValueError("incompatible template IDs")
		if self.instruments != other.instruments:
			raise ValueError("incompatible instrument sets")
		if self.min_instruments != other.min_instruments:
			raise ValueError("incompatible minimum number of instruments")
		if self.delta_t != other.delta_t:
			raise ValueError("incompatible delta_t coincidence thresholds")

		if self.template_ids is None and other.template_ids is not None:
			self.template_ids = frozenset(other.template_ids)

		for key, lnpdf in self.densities.items():
			lnpdf += other.densities[key]

		try:
			del self.interps
		except AttributeError:
			pass
		return self

	def increment(self, event):
		# this test is intended to fail if .template_ids is None:
		# must not collect trigger statistics unless we can verify
		# that they are for the correct templates.
		# FIXME:  use a proper ID column when one is available
		if event.Gamma0 not in self.template_ids:
			raise ValueError("event from wrong template")
		self.densities["%s_snr_chi" % event.ifo].count[event.snr, event.chisq / event.snr**2.] += 1.0

	def copy(self):
		new = type(self)(self.template_ids, self.instruments, self.delta_t, self.min_instruments)
		for key, lnpdf in self.densities.items():
			new.densities[key] = lnpdf.copy()
		return new

	def mkinterps(self):
		self.interps = dict((key, lnpdf.mkinterp()) for key, lnpdf in self.densities.items())

	def finish(self):
		snr_kernel_width_at_8 = 8.,
		chisq_kernel_width = 0.08,
		sigma = 10.
		for key, lnpdf in self.densities.items():
			# construct the density estimation kernel.  be
			# extremely conservative and assume only 1 in 10
			# samples are independent, but assume there are
			# always at least 1e7 samples.
			numsamples = max(lnpdf.array.sum() / 10. + 1., 1e6)
			snr_bins, chisq_bins = lnpdf.bins
			snr_per_bin_at_8 = (snr_bins.upper() - snr_bins.lower())[snr_bins[8.]]
			chisq_per_bin_at_0_02 = (chisq_bins.upper() - chisq_bins.lower())[chisq_bins[0.02]]

			# apply Silverman's rule so that the width scales
			# with numsamples**(-1./6.) for a 2D PDF
			snr_kernel_bins = snr_kernel_width_at_8 / snr_per_bin_at_8 / numsamples**(1./6.)
			chisq_kernel_bins = chisq_kernel_width / chisq_per_bin_at_0_02 / numsamples**(1./6.)

			# check the size of the kernel. We don't ever let
			# it get smaller than the 2.5 times the bin size
			if snr_kernel_bins < 2.5:
				snr_kernel_bins = 2.5
				warnings.warn("Replacing snr kernel bins with 2.5")
			if chisq_kernel_bins < 2.5:
				chisq_kernel_bins = 2.5
				warnings.warn("Replacing chisq kernel bins with 2.5")

			# convolve bin count with density estimation kernel
			rate.filter_array(lnpdf.array, rate.gaussian_window(snr_kernel_bins, chisq_kernel_bins, sigma = sigma))

			# zero everything below the SNR cut-off.  need to
			# do the slicing ourselves to avoid zeroing the
			# at-threshold bin
			lnpdf.array[:lnpdf.bins[0][self.snr_min],:] = 0.

			# normalize what remains
			lnpdf.normalize()
		self.mkinterps()

		#
		# never allow PDFs that have had the density estimation
		# transform applied to be written to disk:  on-disk files
		# must only ever provide raw counts.  also don't allow
		# density estimation to be applied twice
		#

		def to_xml(*args, **kwargs):
			raise NotImplementedError("writing .finish()'ed LnLRDensity object to disk is forbidden")
		self.to_xml = to_xml
		def finish(*args, **kwargs):
			raise NotImplementedError(".finish()ing a .finish()ed LnLRDensity object is forbidden")
		self.finish = finish

	def to_xml(self, name):
		xml = super(LnLRDensity, self).to_xml(name)
		# None is mapped to an empty string
		# FIXME:  glue can now encode None-valued Params, but we
		# don't rely on that mechanism.  switch to it
		xml.appendChild(ligolw_param.Param.from_pyvalue(u"template_ids", ",".join("%d" % template_id for template_id in sorted(self.template_ids if self.template_ids else []))))
		xml.appendChild(ligolw_param.Param.from_pyvalue(u"instruments", lsctables.instrumentsproperty.set(self.instruments)))
		xml.appendChild(ligolw_param.Param.from_pyvalue(u"min_instruments", self.min_instruments))
		xml.appendChild(ligolw_param.Param.from_pyvalue(u"delta_t", self.delta_t))
		for key, lnpdf in self.densities.items():
			# never write PDFs to disk without ensuring the
			# normalization metadata is up to date
			lnpdf.normalize()
			xml.appendChild(lnpdf.to_xml(key))
		return xml

	@classmethod
	def from_xml(cls, xml, name):
		xml = cls.get_xml_root(xml, name)
		self = cls(
			# an empty string is assumed to mean None
			# FIXME:  remove or "" after glue is upgraded
			# FIXME:  glue can now encode None-valued Params,
			# but we don't rely on that mechanism.  switch to
			# it
			template_ids = frozenset(int(i) for i in (ligolw_param.get_pyvalue(xml, u"template_ids") or "").split(",") if i) or None,
			instruments = lsctables.instrumentsproperty.get(ligolw_param.get_pyvalue(xml, u"instruments")),
			delta_t = ligolw_param.get_pyvalue(xml, u"delta_t"),
			min_instruments = ligolw_param.get_pyvalue(xml, u"min_instruments")
		)
		for key in self.densities:
			self.densities[key] = self.densities[key].from_xml(xml, key)
		return self


#
# =============================================================================
#
#                              Numerator Density
#
# =============================================================================
#


class LnSignalDensity(LnLRDensity):
	# load/initialize an SNRPDF instance for use by all instances of
	# this class
	SNRPDF = inspiral_extrinsics.SNRPDF.load()
	assert SNRPDF.snr_cutoff == LnLRDensity.snr_min

	def __init__(self, *args, **kwargs):
		super(LnSignalDensity, self).__init__(*args, **kwargs)

		# install SNR, chi^2 PDF (one for all instruments)
		self.densities = {
			"snr_chi": inspiral_extrinsics.NumeratorSNRCHIPDF(self.snr_chi_binning)
		}

		# record of horizon distances for all instruments in the
		# network
		self.horizon_history = horizonhistory.HorizonHistories((instrument, horizonhistory.NearestLeafTree()) for instrument in self.instruments)

	def __call__(self, segments, snrs, phase, dt, **kwargs):
		assert frozenset(segments) == self.instruments
		# FIXME:  remove V1 from consideration.  delete after O2
		kwargs.pop("V1_snr_chi", None)
		segments = dict(segments)
		segments.pop("V1", None)
		snrs = dict(snrs)
		snrs.pop("V1", None)
		phase = dict(phase)
		phase.pop("V1", None)
		dt = dict(dt)
		dt.pop("V1", None)
		if len(snrs) < self.min_instruments:
			return float("-inf")

		# use volume-weighted average horizon distance over
		# duration of event to estimate sensitivity
		assert all(segments.values()), "encountered trigger with duration = 0"
		horizons = dict((instrument, (self.horizon_history[instrument].functional_integral(map(float, seg), lambda d: d**3.) / float(abs(seg)))**(1./3.)) for instrument, seg in segments.items())

		# compute P(t).  P(t) \propto volume within which a signal
		# will produce a candidate * number of trials \propto cube
		# of distance to which the mininum required number of
		# instruments can see (I think) * number of templates.  we
		# measure the distance in multiples of 150 Mpc just to get
		# a number that will be, typically, a little closer to 1,
		# in lieu of properly normalizating this factor.  we can't
		# normalize the factor because the normalization depends on
		# the duration of the experiment, and that keeps changing
		# when running online, combining offline chunks from
		# different times, etc., and that would prevent us from
		# being able to compare a ln L ranking statistic value
		# defined during one period to ranking statistic values
		# defined in other periods.  by removing the normalization,
		# and leaving this be simply a factor that is proportional
		# to the rate of signals, we can compare ranking statistics
		# across analysis boundaries but we loose meaning in the
		# overall scale:  ln L = 0 is not a special value, as it
		# would be if the numerator and denominator were both
		# normalized properly.
		horizon = sorted(horizons.values())[-self.min_instruments] / 150.
		if not horizon:
			return float("-inf")
		lnP = 3. * math.log(horizon) + math.log(len(self.template_ids))

		# multiply by P(instruments | t) * P(SNRs | t, instruments).
		lnP += self.SNRPDF.lnP_instruments(snrs.keys(), horizons, self.min_instruments) + self.SNRPDF.lnP_snrs(snrs, horizons, self.min_instruments)

		# evaluate dt and dphi parameters
		lnP += inspiral_extrinsics.lnP_dt_dphi_signal(snrs, phase, dt, horizons, self.delta_t)

		# evalute the (snr, \chi^2 | snr) PDFs (same for all
		# instruments)
		interp = self.interps["snr_chi"]
		return lnP + sum(interp(*value) for name, value in kwargs.items() if name.endswith("_snr_chi"))

	def __iadd__(self, other):
		super(LnSignalDensity, self).__iadd__(other)
		self.horizon_history += other.horizon_history
		return self

	def increment(self, *args, **kwargs):
		raise NotImplementedError

	def copy(self):
		new = super(LnSignalDensity, self).copy()
		new.horizon_history = self.horizon_history.copy()
		return new

	def local_mean_horizon_distance(self, gps, window = segments.segment(-32., +2.)):
		# horizon distance window is in seconds.  this is sort of a
		# hack, we should really tie this to each waveform's filter
		# length somehow, but we don't have a way to do that and
		# it's not clear how much would be gained by going to the
		# trouble of implementing that.  I don't even know what to
		# set it to, so for now it's set to something like the
		# typical width of the whitening filter's impulse response.
		t = abs(window)
		vtdict = self.horizon_history.functional_integral_dict(window.shift(float(gps)), lambda D: D**3.)
		return dict((instrument, (vt / t)**(1./3.)) for instrument, vt in vtdict.items())

	def add_signal_model(self, prefactors_range = (0.01, 0.25), df = 40, inv_snr_pow = 4.):
		# normalize to 10 *mi*llion signals.  this count makes the
		# density estimation code choose a suitable kernel size
		inspiral_extrinsics.NumeratorSNRCHIPDF.add_signal_model(self.densities["snr_chi"], 10000000., prefactors_range, df, inv_snr_pow = inv_snr_pow, snr_min = self.snr_min)
		self.densities["snr_chi"].normalize()

	def candidate_count_model(self, rate = 1000.):
		"""
		Compute and return a prediction for the total number of
		above-threshold signal candidates expected.  The rate
		parameter sets the nominal signal rate in units of Gpc^-3
		a^-1.
		"""
		# FIXME:  this needs to understand a mass distribution
		# model and what part of the mass space this numerator PDF
		# is for
		seg = (self.horizon_history.minkey(), self.horizon_history.maxkey())
		V_times_t = self.horizon_history.functional_integral(seg, lambda horizons: sorted(horizons.values())[-self.min_instruments]**3.)
		# Mpc**3 --> Gpc**3, seconds --> years
		V_times_t *= 1e-9 / (86400. * 365.25)
		return V_times_t * rate * len(self.template_ids)

	def random_sim_params(self, sim, horizon_distance = None, snr_efficiency = 1.0, coinc_only = True):
		"""
		Generator that yields an endless sequence of randomly
		generated parameter dictionaries drawn from the
		distribution of parameters expected for the given
		injection, which is an instance of a SimInspiral table row
		object (see glue.ligolw.lsctables.SimInspiral for more
		information).  Each value in the sequence is a tuple, the
		first element of which is the random parameter dictionary
		and the second is 0.

		See also:

		LnNoiseDensity.random_params()

		The sequence is suitable for input to the .ln_lr_samples()
		log likelihood ratio generator.

		Bugs:

		The second element in each tuple in the sequence is merely
		a placeholder, not the natural logarithm of the PDF from
		which the sample has been drawn, as in the case of
		random_params().  Therefore, when used in combination with
		.ln_lr_samples(), the two probability densities computed
		and returned by that generator along with each log
		likelihood ratio value will simply be the probability
		densities of the signal and noise populations at that point
		in parameter space.  They cannot be used to form an
		importance weighted sampler of the log likelihood ratios.
		"""
		# FIXME:  this is still busted since the rewrite

		# FIXME need to add dt and dphi 
		#
		# retrieve horizon distance from history if not given
		# explicitly.  retrieve SNR threshold from class attribute
		# if not given explicitly
		#

		if horizon_distance is None:
			horizon_distance = self.local_mean_horizon_distance(sim.time_geocent)

		#
		# compute nominal SNRs
		#

		cosi2 = math.cos(sim.inclination)**2.
		gmst = lal.GreenwichMeanSiderealTime(sim.time_geocent)
		snr_0 = {}
		for instrument, DH in horizon_distance.items():
			fp, fc = lal.ComputeDetAMResponse(lalsimulation.DetectorPrefixToLALDetector(str(instrument)).response, sim.longitude, sim.latitude, sim.polarization, gmst)
			snr_0[instrument] = snr_efficiency * 8. * DH * math.sqrt(fp**2. * (1. + cosi2)**2. / 4. + fc**2. * cosi2) / sim.distance

		#
		# construct SNR generators, and approximating the SNRs to
		# be fixed at the nominal SNRs construct \chi^2 generators
		#

		def snr_gen(snr):
			rvs = stats.ncx2(2., snr**2.).rvs
			math_sqrt = math.sqrt
			while 1:
				yield math_sqrt(rvs())

		def chi2_over_snr2_gen(instrument, snr):
			rates_lnx = numpy.log(self.injection_rates["%s_snr_chi" % instrument].bins[1].centres())
			# FIXME:  kinda broken for SNRs below self.snr_min
			rates_cdf = self.injection_rates["%s_snr_chi" % instrument][max(snr, self.snr_min),:].cumsum()
			# add a small tilt to break degeneracies then
			# normalize
			rates_cdf += numpy.linspace(0., 0.001 * rates_cdf[-1], len(rates_cdf))
			rates_cdf /= rates_cdf[-1]
			assert not numpy.isnan(rates_cdf).any()

			interp = interpolate.interp1d(rates_cdf, rates_lnx)
			math_exp = math.exp
			random_random = random.random
			while 1:
				yield math_exp(float(interp(random_random())))

		gens = dict(((instrument, "%s_snr_chi" % instrument), (iter(snr_gen(snr)).next, iter(chi2_over_snr2_gen(instrument, snr)).next)) for instrument, snr in snr_0.items())

		#
		# yield a sequence of randomly generated parameters for
		# this sim.
		#

		while 1:
			params = {"snrs": {}}
			instruments = []
			for (instrument, key), (snr, chi2_over_snr2) in gens.items():
				snr = snr()
				if snr < self.snr_min:
					continue
				params[key] = snr, chi2_over_snr2()
				params["snrs"][instrument] = snr
				instruments.append(instrument)
			if coinc_only and len(instruments) < self.denominator.min_instruments:
				continue
			params.horizons = horizon_distance
			yield params, 0.

	def to_xml(self, name):
		xml = super(LnSignalDensity, self).to_xml(name)
		xml.appendChild(self.horizon_history.to_xml(u"horizon_history"))
		return xml

	@classmethod
	def from_xml(cls, xml, name):
		xml = cls.get_xml_root(xml, name)
		self = super(LnSignalDensity, cls).from_xml(xml, name)
		self.horizon_history = horizonhistory.HorizonHistories.from_xml(xml, u"horizon_history")
		return self


class DatalessLnSignalDensity(LnSignalDensity):
	"""
	Stripped-down version of LnSignalDensity for use in estimating
	ranking statistics when no data has been collected from an
	instrument with which to define the ranking statistic.

	Used, for example, to implement low-significance candidate culls,
	etc.

	Assumes all available instruments are on and have the same horizon
	distance, and assess candidates based only on SNR and \chi^2
	distributions.
	"""
	def __init__(self, *args, **kwargs):
		super(DatalessLnSignalDensity, self).__init__(*args, **kwargs)
		# so we're ready to go!
		self.add_signal_model()

	def __call__(self, segments, snrs, phase, dt, **kwargs):
		# evaluate P(t) \propto number of templates
		lnP = math.log(len(self.template_ids))

		# evaluate SNR PDF.  assume all instruments have 100 Mpc
		# horizon distance
		horizons = dict.fromkeys(segments, 100.)
		lnP += self.SNRPDF.lnP_instruments(snrs.keys(), horizons, self.min_instruments) + self.SNRPDF.lnP_snrs(snrs, horizons, self.min_instruments)

		# evalute the (snr, \chi^2 | snr) PDFs (same for all
		# instruments)
		interp = self.interps["snr_chi"]
		return lnP + sum(interp(*value) for name, value in kwargs.items() if name.endswith("_snr_chi"))

	def __iadd__(self, other):
		raise NotImplementedError

	def increment(self, *args, **kwargs):
		raise NotImplementedError

	def copy(self):
		raise NotImplementedError

	def to_xml(self, name):
		# I/O not permitted:  the on-disk version would be
		# indistinguishable from a real ranking statistic and could
		# lead to accidents
		raise NotImplementedError

	@classmethod
	def from_xml(cls, xml, name):
		# I/O not permitted:  the on-disk version would be
		# indistinguishable from a real ranking statistic and could
		# lead to accidents
		raise NotImplementedError


#
# =============================================================================
#
#                             Denominator Density
#
# =============================================================================
#


class LnNoiseDensity(LnLRDensity):
	def __init__(self, *args, **kwargs):
		super(LnNoiseDensity, self).__init__(*args, **kwargs)

		# record of trigger counts vs time for all instruments in
		# the network
		self.triggerrates = trigger_rate.triggerrates((instrument, trigger_rate.ratebinlist()) for instrument in self.instruments)
		# point this to a LnLRDensity object containing the
		# zero-lag densities to mix zero-lag into the model.
		self.lnzerolagdensity = None

		# initialize a CoincRates object.  NOTE:  this is
		# potentially time-consuming.  the current implementation
		# includes hard-coded fast-paths for the standard
		# gstlal-based inspiral pipeline's coincidence and network
		# configurations, but if those change then doing this will
		# suck.  when scipy >= 0.19 becomes available on LDG
		# clusters this issue will go away (can use qhull's
		# algebraic geometry code for the probability
		# calculations).
		self.coinc_rates = snglcoinc.CoincRates(
			instruments = self.instruments,
			delta_t = self.delta_t,
			min_instruments = self.min_instruments
		)

	@property
	def segmentlists(self):
		return self.triggerrates.segmentlistdict()

	def __call__(self, segments, snrs, phase, dt, **kwargs):
		assert frozenset(segments) == self.instruments
		# FIXME:  remove V1 from consideration.  delete after O2
		kwargs.pop("V1_snr_chi", None)
		segments = dict(segments)
		segments.pop("V1", None)
		snrs = dict(snrs)
		snrs.pop("V1", None)
		phase = dict(phase)
		phase.pop("V1", None)
		dt = dict(dt)
		dt.pop("V1", None)
		if len(snrs) < self.min_instruments:
			return float("-inf")

		triggers_per_second_per_template = {}
		for instrument, seg in segments.items():
			try:
				triggers_per_second_per_template[instrument] = self.triggerrates[instrument].density_at(seg[1]) / len(self.template_ids)
			except IndexError:
				# no rate data at segment end-time =
				# instrument was off
				triggers_per_second_per_template[instrument] = 0.
		# sanity check rates
		assert all(triggers_per_second_per_template[instrument] for instrument in snrs), "impossible candidate in %s at %s when rates were %s triggers/s/template" % (", ".join(sorted(snrs)), ", ".join("%s s in %s" % (str(seg[1]), instrument) for instrument, seg in sorted(segments.items())), str(triggers_per_second_per_template))

		# P(t | noise) = (candidates per unit time @ t) / total
		# candidates.  by not normalizing by the total candidates
		# the return value can only ever be proportional to the
		# probability density, but we avoid the problem of the
		# ranking statistic definition changing on-the-fly while
		# running online, allowing candidates collected later to
		# have their ranking statistics compared meaningfully to
		# the values assigned to candidates collected earlier, when
		# the total number of candidates was smaller.
		lnP = math.log(sum(self.coinc_rates.strict_coinc_rates(**triggers_per_second_per_template).values()) * len(self.template_ids))

		# P(instruments | t, noise)
		lnP += self.coinc_rates.lnP_instruments(**triggers_per_second_per_template)[frozenset(snrs)]

		# evaluate dt and dphi parameters
		lnP += inspiral_extrinsics.lnP_dt_dphi_uniform(self.delta_t)

		# evaluate the rest
		interps = self.interps
		return lnP + sum(interps[name](*value) for name, value in kwargs.items() if name in interps)

	def __iadd__(self, other):
		super(LnNoiseDensity, self).__iadd__(other)
		self.triggerrates += other.triggerrates
		return self

	def copy(self):
		new = super(LnNoiseDensity, self).copy()
		new.triggerrates = self.triggerrates.copy()
		# NOTE:  lnzerolagdensity in the copy is reset to None by
		# this operation.  it is left as an exercise to the calling
		# code to re-connect it to the appropriate object if
		# desired.
		return new

	def mkinterps(self):
		#
		# override to mix zero-lag densities in if requested
		#

		if self.lnzerolagdensity is None:
			super(LnNoiseDensity, self).mkinterps()
		else:
			# same as parent class, but with .lnzerolagdensity
			# added
			self.interps = dict((key, (pdf + self.lnzerolagdensity.densities[key]).mkinterp()) for key, pdf in self.densities.items())

	def add_noise_model(self, number_of_events = 10000, prefactors_range = (0.5, 20.), df = 40, inv_snr_pow = 2.):
		#
		# populate snr,chi2 binnings with a slope to force
		# higher-SNR events to be assesed to be more significant
		# when in the regime beyond the edge of measured or even
		# extrapolated background.
		#

		# pick a canonical PDF to definine the binning (we assume
		# they're all the same and only compute this array once to
		# save time
		lnpdf = self.densities.values()[0]
		arr = numpy.zeros_like(lnpdf.array)

		snrindices, rcossindices = lnpdf.bins[self.snr_min:1e10, 1e-6:1e2]
		snr, dsnr = lnpdf.bins[0].centres()[snrindices], lnpdf.bins[0].upper()[snrindices] - lnpdf.bins[0].lower()[snrindices]
		rcoss, drcoss = lnpdf.bins[1].centres()[rcossindices], lnpdf.bins[1].upper()[rcossindices] - lnpdf.bins[1].lower()[rcossindices]

		prcoss = numpy.ones(len(rcoss))
		psnr = 1e-8 * snr**-6 #(1. + 10**6) / (1. + snr**6)
		psnrdcoss = numpy.outer(numpy.exp(-(snr - 2**.5)**2/ 2.) * dsnr, numpy.exp(-(rcoss - .05)**2 / .00015*2) * drcoss)
		arr[snrindices, rcossindices] = psnrdcoss

		# normalize to the requested count.  give 99% of the
		# requested events to this portion of the model
		arr *= 0.99 * number_of_events / arr.sum()

		for lnpdf in self.densities.values():
			# add in the 99% noise model
			lnpdf.array += arr
			# add 1% from the "glitch model"
			inspiral_extrinsics.NumeratorSNRCHIPDF.add_signal_model(lnpdf, n = 0.01 * number_of_events, prefactors_range = prefactors_range, df = df, inv_snr_pow = inv_snr_pow, snr_min = self.snr_min)
			# re-normalize
			lnpdf.normalize()

	def candidate_count_model(self):
		"""
		Compute and return a prediction for the total number of
		noise candidates expected for each instrument
		combination.
		"""
		# assumes the trigger rate is uniformly distributed among
		# the templates and uniform in live time, calculates
		# coincidence rate assuming template exact-match
		# coincidence is required, then multiplies by the template
		# count to get total coincidence rate.
		return dict((instruments, count * len(self.template_ids)) for instruments, count in self.coinc_rates.marginalized_strict_coinc_counts(
			self.triggerrates.segmentlistdict(),
			**dict((instrument, rate / len(self.template_ids)) for instrument, rate in self.triggerrates.densities.items())
		).items())

	def random_params(self):
		"""
		Generator that yields an endless sequence of randomly
		generated candidate parameters.  NOTE: the parameters will
		be within the domain of the repsective binnings but are not
		drawn from the PDF stored in those binnings --- this is not
		an MCMC style sampler.  Each value in the sequence is a
		three-element tuple.  The first two elements of each tuple
		provide the *args and **kwargs values for calls to this PDF
		or the numerator PDF or the ranking statistic object.  The
		final is the natural logarithm (up to an arbitrary
		constant) of the PDF from which the parameters have been
		drawn evaluated at the point described by the *args and
		**kwargs.

		See also:

		random_sim_params()

		The sequence is suitable for input to the .ln_lr_samples()
		log likelihood ratio generator.
		"""
		snr_slope = 0.8 / len(self.instruments)**3

		# FIXME:  the dt portion is only correct for the H1L1 pair
		lnP_dt_dphi = inspiral_extrinsics.lnP_dt_dphi_uniform_H1L1(self.delta_t)
		snrchi2gens = dict((instrument, iter(self.densities["%s_snr_chi" % instrument].bins.randcoord(ns = (snr_slope, 1.), domain = (slice(self.snr_min, None), slice(None, None)))).next) for instrument in self.instruments)
		t_and_rate_gen = iter(self.triggerrates.random_uniform()).next
		t_offsets_gen = dict((instruments, self.coinc_rates.plausible_toas(instruments).next) for instruments in self.coinc_rates.all_instrument_combos)
		random_randint = random.randint
		random_sample = random.sample
		random_uniform = random.uniform
		segment = segments.segment
		twopi = 2. * math.pi
		ln_1_2 = math.log(0.5)
		def nCk(n, k):
			return math.factorial(n) // math.factorial(k) // math.factorial(n - k)
		while 1:
			t, rates, lnP_t = t_and_rate_gen()

			instruments = tuple(instrument for instrument, rate in rates.items() if rate > 0)
			if len(instruments) < self.min_instruments:
				# FIXME:  doing this invalidates lnP_t.  I
				# think the error is merely an overall
				# normalization error, though, and nothing
				# cares about the normalization
				continue
			# to pick instruments, we first pick an integer k
			# between m = min_instruments and n =
			# len(instruments) inclusively, then choose than
			# many unique names from among the available
			# instruments.  the probability of the outcome is
			#
			# = P(k) * P(selection | k)
			# = 1 / (n - m + 1) * 1 / nCk
			#
			# where nCk = number of k choices possible from a
			# collection of n things.
			k = random_randint(self.min_instruments, len(instruments))
			lnP_instruments = -math.log((len(instruments) - self.min_instruments + 1) * nCk(len(instruments), k))
			instruments = frozenset(random_sample(instruments, k))

			seq = sum((snrchi2gens[instrument]() for instrument in instruments), ())
			kwargs = dict(("%s_snr_chi" % instrument, value) for instrument, value in zip(instruments, seq[0::2]))
			# FIXME: waveform duration hard-coded to 10 s,
			# generalize
			kwargs["segments"] = dict.fromkeys(self.instruments, segment(t - 10.0, t))
			kwargs["snrs"] = dict((instrument, kwargs["%s_snr_chi" % instrument][0]) for instrument in instruments)
			# FIXME:  add dt to segments?  not self-consistent
			# if we don't, but doing so screws up the test that
			# was done to check which instruments are on and
			# off at "t"
			kwargs["dt"] = t_offsets_gen[instruments]()
			kwargs["phase"] = dict((instrument, random_uniform(0., twopi)) for instrument in instruments)
			# NOTE:  I think the result of this sum is, in
			# fact, correctly normalized, but nothing requires
			# it to be (only that it be correct up to an
			# unknown constant) and I've not checked that it is
			# so the documentation doesn't promise that it is.
			# FIXME:  no, it's not normalized until the dt_dphi
			# bit is corrected for other than H1L1
			yield (), kwargs, sum(seq[1::2], lnP_t + lnP_instruments + lnP_dt_dphi)

	def to_xml(self, name):
		xml = super(LnNoiseDensity, self).to_xml(name)
		xml.appendChild(self.triggerrates.to_xml(u"triggerrates"))
		return xml

	@classmethod
	def from_xml(cls, xml, name):
		xml = cls.get_xml_root(xml, name)
		self = super(LnNoiseDensity, cls).from_xml(xml, name)
		self.triggerrates = trigger_rate.triggerrates.from_xml(xml, u"triggerrates")
		self.triggerrates.coalesce()	# just in case
		return self


class DatalessLnNoiseDensity(LnNoiseDensity):
	"""
	Stripped-down version of LnNoiseDensity for use in estimating
	ranking statistics when no data has been collected from an
	instrument with which to define the ranking statistic.

	Used, for example, to implement low-significance candidate culls,
	etc.

	Assumes all available instruments are on and have the same horizon
	distance, and assess candidates based only on SNR and \chi^2
	distributions.
	"""
	def __init__(self, *args, **kwargs):
		super(DatalessLnNoiseDensity, self).__init__(*args, **kwargs)
		# this a guess at a mass dependent snr chisq prior.  10
		# *million* events gets the density estimation kernel to be
		# a sensible size.
		# FIXME:  initialize from template information instead of
		# guessing mchirp.
		mchirp = 0.8
		self.add_noise_model(number_of_events = 10000000, prefactors_range = ((1. / mchirp)**.33, 25.), df = 40, inv_snr_pow = 3.)

	def __call__(self, segments, snrs, phase, dt, **kwargs):
		# assume all instruments are on, 1 trigger per second per
		# template
		triggers_per_second_per_template = dict.fromkeys(segments, 1.)

		# P(t | noise) = (candidates per unit time @ t) / total
		# candidates.  by not normalizing by the total candidates
		# the return value can only ever be proportional to the
		# probability density, but we avoid the problem of the
		# ranking statistic definition changing on-the-fly while
		# running online, allowing candidates collected later to
		# have their ranking statistics compared meaningfully to
		# the values assigned to candidates collected earlier, when
		# the total number of candidates was smaller.
		lnP = math.log(sum(self.coinc_rates.strict_coinc_rates(**triggers_per_second_per_template).values()) * len(self.template_ids))

		# P(instruments | t, noise)
		lnP += self.coinc_rates.lnP_instruments(**triggers_per_second_per_template)[frozenset(snrs)]

		# evaluate the SNR, \chi^2 factors
		interps = self.interps
		return lnP + sum(interps[name](*value) for name, value in kwargs.items() if name in interps)

	def random_params(self):
		# won't work
		raise NotImplementedError

	def __iadd__(self, other):
		raise NotImplementedError

	def increment(self, *args, **kwargs):
		raise NotImplementedError

	def copy(self):
		raise NotImplementedError

	def to_xml(self, name):
		# I/O not permitted:  the on-disk version would be
		# indistinguishable from a real ranking statistic and could
		# lead to accidents
		raise NotImplementedError

	@classmethod
	def from_xml(cls, xml, name):
		# I/O not permitted:  the on-disk version would be
		# indistinguishable from a real ranking statistic and could
		# lead to accidents
		raise NotImplementedError
