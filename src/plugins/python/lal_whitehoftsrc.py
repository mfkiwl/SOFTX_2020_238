# Copyright (C) 2010  Leo Singer
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


from gstlal.pipeutil import *


__author__ = "Leo Singer <leo.singer@ligo.org>"
__version__ = "FIXME"
__date__ = "FIXME"


#
# =============================================================================
#
#                                   Element
#
# =============================================================================
#


class lal_whitehoftsrc(gst.Bin):

	__gstdetails__ = (
		'White Noise Source',
		'Source',
		'generate white noise',
		__author__
	)

	gproperty(
		gobject.TYPE_UINT64,
		'samplesperbuffer',
		'Number of samples in each outgoing buffer',
		0, gobject.G_MAXULONG, 16384, # min, max, default
		readable=True, writable=True
	)

	gproperty(
		gobject.TYPE_DOUBLE,
		'volume',
		'Volume of test signal',
		0, 1, 0.8, # min, max, default
		readable=True, writable=True
	)

	def do_set_property(self, prop, val):
		if prop.name in ('samplesperbuffer', 'volume'):
			self.__src.set_property(prop.name, val)
		else:
			super(lal_whitehoftsrc, self).set_property(prop.name, val)

	def do_get_property(self, prop):
		if prop.name in ('samplesperbuffer', 'volume'):
			return self.__src.get_property(prop.name)
		else:
			return super(lal_whitehoftsrc, self).get_property(prop.name)

	def __init__(self):
		super(lal_whitehoftsrc, self).__init__()
		elems = (
			mkelem('audiotestsrc', {'wave': 9, 'samplesperbuffer': 16384}),
			mkelem('capsfilter', {'caps': gst.Caps('audio/x-raw-float, width=64, rate=16384')})
		)
		self.add_many(*elems)
		gst.element_link_many(*elems)
		self.add_pad(gst.GhostPad('src', elems[1].get_static_pad('src')))
		self.__src = elems[0]
	__init__ = with_construct_properties(__init__)



# Register element class
gobject.type_register(lal_whitehoftsrc)
__gstelementfactory__ = ('lal_whitehoftsrc', gst.RANK_NONE, lal_whitehoftsrc)
