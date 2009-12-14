/*
 * Fix broken discontinuity flags
 *
 * Copyright (C) 2009  Kipp Cannon
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 */


/*
 * ========================================================================
 *
 *                                  Preamble
 *
 * ========================================================================
 */


/*
 * stuff from glib/gstreamer
 */


#include <glib.h>
#include <gst/gst.h>


/*
 * our own stuff
 */


#include <gstlal.h>
#include <gstlal_nofakedisconts.h>


/*
 * ============================================================================
 *
 *                                  Sink Pad
 *
 * ============================================================================
 */


/*
 * getcaps()
 */


static GstCaps *getcaps(GstPad * pad)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(gst_pad_get_parent(pad));
	GstCaps *peercaps, *caps;

	/*
	 * get our own allowed caps.  use the fixed caps function to avoid
	 * recursing back into this function.
	 */

	caps = gst_pad_get_fixed_caps_func(pad);

	/*
	 * get the allowed caps from the downstream peer if the peer has
	 * caps, intersect without our own.
	 */

	peercaps = gst_pad_peer_get_caps(element->srcpad);
	if(peercaps) {
		GstCaps *result = gst_caps_intersect(peercaps, caps);
		gst_caps_unref(peercaps);
		gst_caps_unref(caps);
		caps = result;
	}

	/*
	 * done
	 */

	gst_object_unref(element);
	return caps;
}


/*
 * setcaps()
 */


static gboolean setcaps(GstPad *pad, GstCaps *caps)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(gst_pad_get_parent(pad));
	gboolean success;

	/*
	 * try setting caps on downstream element
	 */

	success = gst_pad_set_caps(element->srcpad, caps);

	/*
	 * done
	 */

	gst_object_unref(element);
	return success;
}


/*
 * chain()
 */


static GstFlowReturn chain(GstPad *pad, GstBuffer *buf)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(gst_pad_get_parent(pad));
	GstFlowReturn result;

	if(element->next_offset != GST_BUFFER_OFFSET_NONE) {
		gboolean is_discont = GST_BUFFER_IS_DISCONT(buf);

		if(GST_BUFFER_OFFSET(buf) != element->next_offset || GST_BUFFER_TIMESTAMP(buf) != element->next_timestamp) {
			if(!is_discont) {
				buf = gst_buffer_make_metadata_writable(buf);
				GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
				fprintf(stderr, "%s: set missing discontinuity flag\n", gst_element_get_name(element));
			}
		} else {
			if(is_discont) {
				buf = gst_buffer_make_metadata_writable(buf);
				GST_BUFFER_FLAG_UNSET(buf, GST_BUFFER_FLAG_DISCONT);
				fprintf(stderr, "%s: cleared improper discontinuity flag\n", gst_element_get_name(element));
			}
		}
	}

	element->next_offset = GST_BUFFER_OFFSET_END(buf);
	element->next_timestamp = GST_BUFFER_TIMESTAMP(buf) + GST_BUFFER_DURATION(buf);

	result = gst_pad_push(element->srcpad, buf);

	gst_object_unref(element);
	return result;
}


/*
 * ============================================================================
 *
 *                                Type Support
 *
 * ============================================================================
 */


/*
 * Parent class.
 */


static GstElementClass *parent_class = NULL;


/*
 * Instance finalize function.  See ???
 */


static void finalize(GObject *object)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(object);

	gst_object_unref(element->sinkpad);
	element->sinkpad = NULL;
	gst_object_unref(element->srcpad);
	element->srcpad = NULL;

	G_OBJECT_CLASS(parent_class)->finalize(object);
}


/*
 * Base init function.  See
 *
 * http://developer.gnome.org/doc/API/2.0/gobject/gobject-Type-Information.html#GBaseInitFunc
 */


static void base_init(gpointer class)
{
	static GstElementDetails plugin_details = {
		"Discontinuity flag fix",
		"Filter",
		"Fix incorrectly-set discontinuity flags",
		"Kipp Cannon <kipp.cannon@ligo.org>"
	};
	GstElementClass *element_class = GST_ELEMENT_CLASS(class);

	gst_element_class_set_details(element_class, &plugin_details);

	gst_element_class_add_pad_template(
		element_class,
		gst_pad_template_new(
			"sink",
			GST_PAD_SINK,
			GST_PAD_ALWAYS,
			gst_caps_from_string(
				"ANY"
			)
		)
	);
	gst_element_class_add_pad_template(
		element_class,
		gst_pad_template_new(
			"src",
			GST_PAD_SRC,
			GST_PAD_ALWAYS,
			gst_caps_from_string(
				"ANY"
			)
		)
	);
}


/*
 * Class init function.  See
 *
 * http://developer.gnome.org/doc/API/2.0/gobject/gobject-Type-Information.html#GClassInitFunc
 */


static void class_init(gpointer klass, gpointer class_data)
{
	GObjectClass *gobject_class = G_OBJECT_CLASS(klass);

	parent_class = g_type_class_ref(GST_TYPE_ELEMENT);

	gobject_class->finalize = GST_DEBUG_FUNCPTR(finalize);
}


/*
 * Instance init function.  See
 *
 * http://developer.gnome.org/doc/API/2.0/gobject/gobject-Type-Information.html#GInstanceInitFunc
 */


static void instance_init(GTypeInstance *object, gpointer klass)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(object);
	GstPad *pad;

	gst_element_create_all_pads(GST_ELEMENT(element));

	/* configure (and ref) sink pad */
	pad = gst_element_get_static_pad(GST_ELEMENT(element), "sink");
	gst_pad_set_getcaps_function(pad, GST_DEBUG_FUNCPTR(getcaps));
	gst_pad_set_setcaps_function(pad, GST_DEBUG_FUNCPTR(setcaps));
	gst_pad_set_chain_function(pad, GST_DEBUG_FUNCPTR(chain));
	element->sinkpad = pad;

	/* retrieve (and ref) src pad */
	element->srcpad = gst_element_get_static_pad(GST_ELEMENT(element), "src");

	/* internal data */
	element->next_offset = GST_BUFFER_OFFSET_NONE;
	element->next_timestamp = GST_CLOCK_TIME_NONE;
}


/*
 * gstlal_nofakedisconts_get_type().
 */


GType gstlal_nofakedisconts_get_type(void)
{
	static GType type = 0;

	if(!type) {
		static const GTypeInfo info = {
			.class_size = sizeof(GSTLALNoFakeDiscontsClass),
			.class_init = class_init,
			.base_init = base_init,
			.instance_size = sizeof(GSTLALNoFakeDisconts),
			.instance_init = instance_init,
		};
		type = g_type_register_static(GST_TYPE_ELEMENT, "lal_nofakedisconts", &info, 0);
	}

	return type;
}
