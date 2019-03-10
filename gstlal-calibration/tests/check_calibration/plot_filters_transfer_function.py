#!/usr/bin/env python
# Copyright (C) 2018  Aaron Viets
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
#				   Preamble
#
# =============================================================================
#


import sys
import os
import numpy
from math import pi
import resource
import matplotlib
from matplotlib import rc
rc('text', usetex = True)
matplotlib.rcParams['font.family'] = 'Times New Roman'
matplotlib.rcParams['font.size'] = 16
matplotlib.rcParams['legend.fontsize'] = 14
matplotlib.rcParams['mathtext.default'] = 'regular'
matplotlib.use('Agg')
import glob
import matplotlib.pyplot as plt

from optparse import OptionParser, Option

parser = OptionParser()
parser.add_option("--tf-file-directory", metavar = "directory", default = '.', help = "location of txt files with transfer functions (Default is current directory, '.')")
parser.add_option("--response-model-jump-delay", metavar = "seconds", type = float, default = 0.000061035, help = "Time delay in time-stamping DARM_ERR (Default is one 16384-Hz clock cycle)")
parser.add_option("--filters-model-jump-delay", metavar = "seconds", type = float, default = 0.0, help = "Any time delay in time-stamping DARM_ERR not contained in the model in the filters file (Default is 0.0 seconds).")
parser.add_option("--tf-frequency-min", type = float, default = -1, help = "Minimum frequency for transfer function plots (Default is to disable)")
parser.add_option("--tf-frequency-max", type = float, default = -1, help = "Maximum frequency for transfer function plots (Default is to disable)")
parser.add_option("--tf-frequency-scale", default = "log", help = "Frequency scale for transfer function plots (linear or log, default is log)")
parser.add_option("--tf-magnitude-min", type = float, default = -1, help = "Minimum magnitude for transfer function plots (Default is to disable)")
parser.add_option("--tf-magnitude-max", type = float, default = -1, help = "Maximum magnitude for transfer function plots (Default is to disable)")
parser.add_option("--tf-magnitude-scale", default = "log", help = "Magnitude scale for transfer function plots (linear or log, default is log)")
parser.add_option("--tf-phase-min", metavar = "degrees", type = float, default = 1000, help = "Minimum phase for transfer function plots, in degrees (Default is to disable)")
parser.add_option("--tf-phase-max", metavar = "degrees", type = float, default = 1000, help = "Maximum phase for transfer function plots, in degrees (Default is to disable)")
parser.add_option("--ratio-frequency-min", type = float, default = -1, help = "Minimum frequency for transfer function ratio plots (Default is to disable)")
parser.add_option("--ratio-frequency-max", type = float, default = -1, help = "Maximum frequency for transfer function ratio plots (Default is to disable)")
parser.add_option("--ratio-frequency-scale", default = "log", help = "Frequency scale for transfer function ratio plots (linear or log, default is log)")
parser.add_option("--ratio-magnitude-min", type = float, default = -1, help = "Minimum magnitude for transfer function ratio plots (Default is to disable)")
parser.add_option("--ratio-magnitude-max", type = float, default = -1, help = "Maximum magnitude for transfer function ratio plots (Default is to disable)")
parser.add_option("--ratio-magnitude-scale", default = "linear", help = "Magnitude scale for transfer function ratio plots (linear or log, default is linear)")
parser.add_option("--ratio-phase-min", metavar = "degrees", type = float, default = 1000, help = "Minimum phase for transfer function ratio plots, in degrees (Default is to disable)")
parser.add_option("--ratio-phase-max", metavar = "degrees", type = float, default = 1000, help = "Maximum phase for transfer function ratio plots, in degrees (Default is to disable)")

options, filenames = parser.parse_args()

#
# Load in the filters file that contains filter coefficients, etc.
# Search the directory tree for files with names matching the one we want.
#

# Identify any files that have filters transfer functions in them
tf_files = [f for f in os.listdir(options.tf_file_directory) if (os.path.isfile(f) and ('GDS' in f or 'DCS' in f) and 'npz' in f and 'filters_transfer_function' in f and '.txt' in f)]
for tf_file in tf_files:
	filters_name = tf_file.split('_npz')[0] + '.npz'
	# Search the directory tree for filters files with names matching the one we want.
	filters_paths = []
	print("\nSearching for %s ..." % filters_name)
	# Check the user's home directory
	for dirpath, dirs, files in os.walk(os.environ['HOME']):
		if filters_name in files:
			# We prefer filters that came directly from a GDSFilters directory of the calibration SVN
			if dirpath.count("GDSFilters") > 0:
				filters_paths.insert(0, os.path.join(dirpath, filters_name))
			else:
				filters_paths.append(os.path.join(dirpath, filters_name))
	# Check if there is a checkout of the entire calibration SVN
	for dirpath, dirs, files in os.walk('/ligo/svncommon/CalSVN/aligocalibration/trunk/Runs/'):
		if filters_name in files:
			# We prefer filters that came directly from a GDSFilters directory of the calibration SVN
			if dirpath.count("GDSFilters") > 0:
				filters_paths.insert(0, os.path.join(dirpath, filters_name))
			else:
				filters_paths.append(os.path.join(dirpath, filters_name))
	if not len(filters_paths):
		raise ValueError("Cannot find filters file %s in home directory %s or in /ligo/svncommon/CalSVN/aligocalibration/trunk/Runs/*/GDSFilters", (filters_name, os.environ['HOME']))
	print("Loading calibration filters from %s\n" % filters_paths[0])
	filters = numpy.load(filters_paths[0])

	model_jump_delay = options.filters_model_jump_delay
	# Determine what transfer function this is
	if '_tst_' in tf_file and 'DCS' in tf_file:
		plot_title = "TST Transfer Function"
		model_name = "tst_model" if "tst_model" in filters else None
	elif '_tst_' in tf_file and 'GDS' in tf_file:
		plot_title = "TST Correction Transfer Function"
		model_name = "ctrl_corr_model" if "ctrl_corr_model" in filters else None
	elif '_pum_' in tf_file and 'DCS' in tf_file:
		plot_title = "PUM Transfer Function"
		model_name = "pum_model" if "pum_model" in filters else None
	elif '_pum_' in tf_file and 'GDS' in tf_file:
		plot_title = "PUM Correction Transfer Function"
		model_name = "ctrl_corr_model" if "ctrl_corr_model" in filters else None
	elif '_uim_' in tf_file and 'DCS' in tf_file:
		plot_title = "UIM Transfer Function"
		model_name = "uim_model" if "uim_model" in filters else None
	elif '_uim_' in tf_file and 'GDS' in tf_file:
		plot_title = "UIM Correction Transfer Function"
		model_name = "ctrl_corr_model" if "ctrl_corr_model" in filters else None
	elif '_pumuim_' in tf_file and 'DCS' in tf_file:
		plot_title = "PUM/UIM Transfer Function"
		model_name = "pumuim_model" if "pumuim_model" in filters else None
	elif '_pumuim_' in tf_file and 'GDS' in tf_file:
		plot_title = "PUM/UIM Correction Transfer Function"
		model_name = "ctrl_corr_model" if "ctrl_corr_model" in filters else None
	elif '_res_' in tf_file and 'DCS' in tf_file:
		plot_title = "Inverse Sensing Transfer Function"
		model_name = "invsens_model" if "invsens_model" in filters else None
	elif '_res_' in tf_file and 'GDS' in tf_file:
		plot_title = "Inverse Sensing Correction Transfer Function"
		model_name = "res_corr_model" if "res_corr_model" in filters else None
	elif '_response_' in tf_file:
		plot_title = "Response Function"
		model_name = "response_function" if "response_function" in filters else None
		model_jump_delay = options.response_model_jump_delay
	else:
		plot_title = "Transfer Function"
		model_name = None

	ifo = ''
	if 'H1' in tf_file:
		ifo = 'H1 '
	if 'L1' in tf_file:
		ifo = 'L1 '

	# Remove unwanted lines from transfer function file, and re-format wanted lines
	f = open(tf_file, 'r')
	lines = f.readlines()
	f.close()
	tf_length = len(lines) - 5
	f = open(tf_file.replace('.txt', '_reformatted.txt'), 'w')
	for j in range(3, 3 + tf_length):
		f.write(lines[j].replace(' + ', '\t').replace(' - ', '\t-').replace('i', ''))
	f.close()

	# Read data from re-formatted file and find frequency vector, magnitude, and phase
	data = numpy.loadtxt(tf_file.replace('.txt', '_reformatted.txt'))
	os.remove(tf_file.replace('.txt', '_reformatted.txt'))
	filters_tf = []
	frequency = []
	magnitude = []
	phase = []
	df = data[1][0] - data[0][0] # frequency spacing
	for j in range(0, len(data)):
		frequency.append(data[j][0])
		tf_at_f = (data[j][1] + 1j * data[j][2]) * numpy.exp(-2j * numpy.pi * data[j][0] * model_jump_delay)
		filters_tf.append(tf_at_f)
		magnitude.append(abs(tf_at_f))
		phase.append(numpy.angle(tf_at_f) * 180.0 / numpy.pi)

	# Find frequency-domain models in filters file if they are present and resample if necessary
	if model_name is not None:
		model = filters[model_name]
		model_tf = []
		model_magnitude = []
		model_phase = []
		ratio = []
		ratio_magnitude = []
		ratio_phase = []
		# Check the frequency spacing of the model
		model_df = model[0][1] - model[0][0]
		cadence = df / model_df
		index = 0
		# This is a linear resampler (it just connects the dots with straight lines)
		while index < tf_length:
			before_idx = numpy.floor(cadence * index)
			after_idx = numpy.ceil(cadence * index + 1e-10)
			# Check if we've hit the end of the model transfer function
			if after_idx >= len(model[0]):
				if before_idx == cadence * index:
					model_tf.append(model[1][before_idx] + 1j * model[2][before_idx])
					model_magnitude.append(abs(model_tf[index]))
					model_phase.append(numpy.angle(model_tf[index]) * 180.0 / numpy.pi)
					ratio.append(filters_tf[index] / model_tf[index])
					ratio_magnitude.append(abs(ratio[index]))
					ratio_phase.append(numpy.angle(ratio[index]) * 180.0 / numpy.pi)
				index = tf_length
			else:
				before = model[1][before_idx] + 1j * model[2][before_idx]
				after = model[1][after_idx] + 1j * model[2][after_idx]
				before_weight = after_idx - cadence * index
				after_weight = cadence * index - before_idx
				model_tf.append(before_weight * before + after_weight * after)
				model_magnitude.append(abs(model_tf[index]))
				model_phase.append(numpy.angle(model_tf[index]) * 180.0 / numpy.pi)
				ratio.append(filters_tf[index] / model_tf[index])
				ratio_magnitude.append(abs(ratio[index]))
				ratio_phase.append(numpy.angle(ratio[index]) * 180.0 / numpy.pi)
				index += 1

	# Filter transfer function plots
	plt.figure(figsize = (10, 8))
	if model_name is not None:
		plt.subplot(221)
		plt.plot(frequency, model_magnitude, 'orange', linewidth = 1.0, label = r'${\rm L1 \ Model \ response}$')
		leg = plt.legend(fancybox = True)
		leg.get_frame().set_alpha(0.5)
		plt.gca().set_xscale(options.tf_frequency_scale)
		plt.gca().set_yscale(options.tf_magnitude_scale)
		#plt.title(plot_title)
		plt.ylabel(r'${\rm Magnitude \ [m/ct]}$')
		if options.tf_frequency_max > 0:
			plt.xlim(options.tf_frequency_min, options.tf_frequency_max)
		if options.tf_magnitude_max > 0:
			plt.ylim(options.tf_magnitude_min, options.tf_magnitude_max)
		plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
		ax = plt.subplot(223)
		ax.set_xscale(options.tf_frequency_scale)
		plt.plot(frequency, model_phase, 'orange', linewidth = 1.0)
		plt.ylabel(r'${\rm Phase \ [deg]}$')
		plt.xlabel(r'${\rm Frequency \ [Hz]}$')
		if options.tf_frequency_max > 0:
			plt.xlim(options.tf_frequency_min, options.tf_frequency_max)
		if options.tf_phase_max < 1000:
			plt.ylim(options.tf_phase_min, options.tf_phase_max)
		plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
	plt.subplot(221)
	plt.plot(frequency, magnitude, 'royalblue', linewidth = 1.0, label = r'${\rm L1 \ Filters \ response}$')
	leg = plt.legend(fancybox = True)
	leg.get_frame().set_alpha(0.5)
	plt.gca().set_xscale(options.tf_frequency_scale)
	plt.gca().set_yscale(options.tf_magnitude_scale)
	#plt.title(plot_title)
	plt.ylabel(r'${\rm Magnitude \ [m/ct]}$')
	if options.tf_frequency_max > 0:
		plt.xlim(options.tf_frequency_min, options.tf_frequency_max)
	if options.tf_magnitude_max > 0:
		plt.ylim(options.tf_magnitude_min, options.tf_magnitude_max)
	plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
	ax = plt.subplot(223)
	ax.set_xscale(options.tf_frequency_scale)
	plt.plot(frequency, phase, 'royalblue', linewidth = 1.0)
	plt.ylabel(r'${\rm Phase [deg]}$')
	plt.xlabel(r'${\rm Frequency \ [Hz]}$')
	if options.tf_frequency_max > 0:
		plt.xlim(options.tf_frequency_min, options.tf_frequency_max)
	if options.tf_phase_max < 1000:
		plt.ylim(options.tf_phase_min, options.tf_phase_max)
	plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
	#plt.savefig(tf_file.replace('.txt', '.png'))
	#plt.savefig(tf_file.replace('.txt', '.pdf'))

	# Plots of the ratio filters / model
	if model_name is not None:
		#plt.figure(figsize = (10, 12))
		plt.subplot(222)
		plt.plot(frequency, ratio_magnitude, 'royalblue', linewidth = 1.0, label = r'${\rm L1 \ Filters / Model}$')
		leg = plt.legend(fancybox = True)
		leg.get_frame().set_alpha(0.5)
		plt.gca().set_xscale(options.ratio_frequency_scale)
		plt.gca().set_yscale(options.ratio_magnitude_scale)
		#plt.title(plot_title)
		#plt.ylabel(r'${\rm Magnitude \ [m/ct]}$')
		if options.ratio_frequency_max > 0:
			plt.xlim(options.ratio_frequency_min, options.ratio_frequency_max)
		if options.ratio_magnitude_max > 0:
			plt.ylim(options.ratio_magnitude_min, options.ratio_magnitude_max)
		plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
		ax = plt.subplot(224)
		ax.set_xscale(options.ratio_frequency_scale)
		plt.plot(frequency, ratio_phase, 'royalblue', linewidth = 1.0)
		#plt.ylabel(r'${\rm Phase \ [deg]}$')
		plt.xlabel(r'${\rm Frequency \ [Hz]}$')
		if options.ratio_frequency_max > 0:
			plt.xlim(options.ratio_frequency_min, options.ratio_frequency_max)
		if options.ratio_phase_max < 1000:
			plt.ylim(options.ratio_phase_min, options.ratio_phase_max)
		plt.grid(True, which = "both", linestyle = ':', linewidth = 0.3, color = 'black')
		plt.savefig(tf_file.replace('.txt', '_ratio.png'))
		plt.savefig(tf_file.replace('.txt', '_ratio.pdf'))

