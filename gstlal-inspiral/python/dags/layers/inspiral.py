# Copyright (C) 2020  Patrick Godwin (patrick.godwin@ligo.org)
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


import os

from gstlal import plugins
from gstlal.config import Argument, Option
from gstlal.dags.layers import Layer, Node
from gstlal.dags import util as dagutils


def reference_psd_layer(config, dag, time_bins):
	layer = Layer("gstlal_reference_psd", requirements=config.condor, base_layer=True)

	for ifo in config.ifos:
		for span in time_bins:
			start, end = span
			psd_path = data_path("psd", start)
			psd_file = dagutils.T050017_filename(ifo, "REFERENCE_PSD", span, '.xml.gz')

			layer += Node(
				arguments = [
					Option("gps-start-time", int(start)),
					Option("gps-end-time", int(end)),
					Option("data-source", "frames"),
					Option("channel-name", format_ifo_args(ifo, config.source.channel_name)),
					Option("frame-type", format_ifo_args(ifo, config.source.frame_type)),
					Option("frame-segments-name", config.source.frame_segments_name),
					Option("data-find-server", config.source.data_find_server),
					Option("psd-fft-length", config.psd.fft_length),
				],
				inputs = [
					Option("frame-segments-file", config.source.frame_segments_file)
				],
				outputs = [
					Option("write-psd", os.path.join(psd_path, psd_file))
				],
			)

	return layer


def median_psd_layer(config, dag):
	layer = Layer("gstlal_median_of_psds", requirements=config.condor)

	median_path = data_path("median_psd", config.start)
	median_file = dagutils.T050017_filename(config.ifos, "REFERENCE_PSD", config.span, '.xml.gz')

	layer += Node(
		inputs = [Argument("psds", dag["reference_psd"].outputs["write-psd"])],
		outputs = [Option("output-name", os.path.join(median_path, median_file))]
	)

	return layer


def svd_bank_layer(config, dag, svd_bins):
	layer = Layer("gstlal_inspiral_svd_bank", requirements=config.condor)

	for svd_bin in svd_bins:
		svd_path = data_path("svd_bank", config.start)
		svd_file = dagutils.T050017_filename(config.ifos, f"{svd_bin}_SVD", config.span, '.xml.gz')

		layer += Node(
			arguments = [
				Option("svd-tolerance", config.svd.tolerance),
				Option("flow", config.svd.f_low),
				Option("sample-rate", config.svd.sample_rate),
				Option("samples-min", config.svd.samples_min),
				Option("samples-max-64", config.svd.samples_max_64),
				Option("samples-max-256", config.svd.samples_max_256),
				Option("samples-max", config.svd.samples_max),
				Option("autocorrelation-length", config.svd.autocorrelation_length),
				Option("bank-id", svd_bin),
			],
			inputs = [Option("reference-psd", dag["median_psd"].outputs["output-name"])],
			outputs = [Option("write-svd", os.path.join(svd_path, svd_file))],
		)

	return layer


def filter_layer(config, dag, time_bins, svd_bins):
	layer = Layer("gstlal_inspiral", requirements=config.condor)

	common_opts = [
		Option("track-psd"),
		Option("local-frame-caching"),
		Option("data-source", "frames"),
		Option("psd-fft-length", config.psd.fft_length),
		Option("channel-name", format_ifo_args(config.ifos, config.source.channel_name)),
		Option("frame-type", format_ifo_args(config.ifos, config.source.frame_type)),
		Option("data-find-server", config.source.data_find_server),
		Option("frame-segments-name", config.source.frame_segments_name),
		Option("tmp-space", dagutils.condor_scratch_space()),
		Option("control-peak-time", config.filter.control_peak_time),
		Option("coincidence-threshold", config.filter.coincidence_threshold),
		Option("singles-threshold", config.filter.singles_threshold),
		Option("fir-stride", config.filter.fir_stride),
		Option("min-instruments", config.filter.min_instruments),
		Option("reference-likelihood-file", config.filter.reference_likelihood_file),
	]

	# disable service discovery if using singularity
	if config.condor.singularity_image:
		common_opts.append(Option("disable-service-discovery"))

	for time_idx, span in enumerate(time_bins):
		start, end = span
		for svd_idx, svd_bin in enumerate(svd_bins):
			filter_opts = [
				#Option("ht-gate-threshold", config.filter.gate_threshold[svd_bin]),
				Option("ht-gate-threshold", config.filter.ht_gate_threshold),
				Option("gps-start-time", int(start)),
				Option("gps-end-time", int(end)),
			]
			filter_opts.extend(common_opts)

			trigger_path = data_path("triggers", start)
			dist_stat_path = data_path("dist_stats", start)
			trigger_file = dagutils.T050017_filename(config.ifos, f"{svd_bin}_LLOID", span, '.xml.gz')
			dist_stat_file = dagutils.T050017_filename(config.ifos, f"{svd_bin}_DIST_STATS", span, '.xml.gz')

			layer += Node(
				arguments = filter_opts,
				inputs = [
					Option("frame-segments-file", config.source.frame_segments_file),
					Option("veto-segments-file", config.filter.veto_segments_file),
					Option("reference-psd", dag["reference_psd"].outputs["write-psd"][time_idx]),
					Option("svd-bank", dag["svd_bank"].outputs["write-svd"][svd_idx]),
					Option("time-slide-file", config.filter.time_slide_file),
				],
				outputs = [
					Option("output", trigger_file),
					Option("ranking-stat-output", dist_stat_file),
				],
			)

	return layer


def aggregate_layer(config, dag, time_bins):
	layer = Layer("gstlal_inspiral_aggregate", requirements=config.condor)

	return layer


def data_path(data_name, start, create=True):
	path = os.path.join(data_name, dagutils.gps_directory(start))
	os.makedirs(path, exist_ok=True)
	return path


def format_ifo_args(ifos, args):
	if isinstance(ifos, str):
		ifos = [ifos]
	return [f"{ifo}={args[ifo]}" for ifo in ifos]


@plugins.register
def layers():
	return {
		"reference_psd": reference_psd_layer,
		"median_psd": median_psd_layer,
		"svd_bank": svd_bank_layer,
		"filter": filter_layer,
		"aggregate": aggregate_layer,
	}
