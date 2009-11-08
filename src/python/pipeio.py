# Copyright (C) 2009  LIGO Scientific Collaboration
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


import numpy


from pylal import datatypes as laltypes


__author__ = "Kipp Cannon <kipp.cannon@ligo.org>, Chad Hanna <chad.hanna@ligo.org>, Drew Keppel <drew.keppel@ligo.org>"
__version__ = "FIXME"
__date__ = "FIXME"


#
# =============================================================================
#
#                                   Buffers
#
# =============================================================================
#


def array_from_audio_buffer(buf):
	caps = buf.caps[0]
	name = caps.get_name()
	if name == "audio/x-raw-float":
		dtype = "f%d" % (caps["width"] / 8)
	elif name == "audio/x-raw-int":
		if caps["signed"]:
			dtype = "i%d" % (caps["width"] / 8)
		else:
			dtype = "s%d" % (caps["width"] / 8)
	elif name == "audio/x-raw-complex":
		dtype = "c%d" % (caps["width"] / 8)
	else:
		raise ValueError, dtype
	channels = caps["channels"]

	a = numpy.frombuffer(buf, dtype = dtype)

	return numpy.reshape(a, (len(a) / channels, channels))


#
# =============================================================================
#
#                                   Messages
#
# =============================================================================
#


def parse_spectrum_message(message):
	"""
	Parse a "spectrum" message from the lal_whiten element, return a
	LAL REAL8FrequencySeries containing the strain spectral density.
	"""
	return laltypes.REAL8FrequencySeries(
		name = "PSD",
		epoch = laltypes.LIGOTimeGPS(0, message.structure["timestamp"]),
		f0 = 0.0,
		deltaF = message.structure["delta-f"],
		sampleUnits = laltypes.LALUnit(message.structure["sample-units"]),
		data = numpy.array(message.structure["magnitude"])
	)


#
# =============================================================================
#
#                                     Tags
#
# =============================================================================
#


def parse_framesrc_tags(taglist):
	if "instrument" in taglist:
		instrument = taglist["instrument"]
	else:
		instrument = None
	if "channel-name" in taglist:
		channel_name = taglist["channel-name"]
	else:
		channel_name = None
	if "units" in taglist:
		sample_units = laltypes.LALUnit(taglist["units"].strip())
	else:
		sample_units = None
	return {
		"instrument": instrument,
		"channel-name": channel_name,
		"sample-units": sample_units
	}
