/*
 * Fix broken discontinuity flags
 *
 * Copyright (C) 2009--2013  Kipp Cannon
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


/**
 * SECTION:gstlal_nofakedisconts
 * @short_description:  Fix broken GST_BUFFER_FLAG_DISCONT flags.
 *
 * The GStreamer base class #GstBaseTransform requires one (or more) output
 * buffers to be produced by subclasses for every input buffer received.
 * If the subclass fails to produce an output buffer this is remembered
 * by #GstBaseTransform which then sets the #GST_BUFFER_FLAG_DISCONT flag
 * on the next output buffer, regardless of whether or not the buffer is,
 * infact, discontinuous with any previous buffer.  This is annoying, and
 * since other elements often respond to the discontinuity by resetting
 * themselves it creates problems.  This bug aflicts several stock elements
 * such as the audioresampler.  To work around the problem, this element is
 * available.  This element monitors the data stream, watching timestamps
 * and offsets, and sets or clears the discontinuity flag on each buffer
 * based on exactly whether it is discontinuous with the previous buffer.
 *
 * Reviewed:  3d2cf9ea32085a2dd4854cb71b1cbaaf5547fc57 2014-08-12 K.
 * Cannon, J.  Creighton, B. Sathyaprakash.
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


#include <gstlal/gstlal_debug.h>
#include <gstlal_nofakedisconts.h>


/*
 * parameters
 */


#define DEFAULT_SILENT FALSE


/*
 * ========================================================================
 *
 *                                Boilerplate
 *
 * ========================================================================
 */


G_DEFINE_TYPE(
	GSTLALNoFakeDisconts,
	gstlal_nofakedisconts,
	GST_TYPE_ELEMENT
);


/*
 * ============================================================================
 *
 *                                    Pads
 *
 * ============================================================================
 */


/*
 * chain()
 */


static GstFlowReturn chain(GstPad *pad, GstObject *parent, GstBuffer *buf)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(parent);
	GstFlowReturn result;
	if(element->next_offset != GST_BUFFER_OFFSET_NONE) {
		gboolean is_discont = GST_BUFFER_IS_DISCONT(buf);

		if(GST_BUFFER_OFFSET(buf) != element->next_offset || GST_BUFFER_PTS(buf) != element->next_timestamp) {
			if(!is_discont) {
				buf = gst_buffer_make_writable(buf);
				GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
				if(!element->silent)
					fprintf(stderr, "%s: set missing discontinuity flag at %" GST_TIME_SECONDS_FORMAT "\n", gst_element_get_name(element), GST_TIME_SECONDS_ARGS(GST_BUFFER_PTS(buf)));
			}
		} else {
			if(is_discont) {
				buf = gst_buffer_make_writable(buf);
				GST_BUFFER_FLAG_UNSET(buf, GST_BUFFER_FLAG_DISCONT);
				if(!element->silent)
					fprintf(stderr, "%s: cleared improper discontinuity flag at %" GST_TIME_SECONDS_FORMAT "\n", gst_element_get_name(element), GST_TIME_SECONDS_ARGS(GST_BUFFER_PTS(buf)));
			}
		}
	}

	element->next_offset = GST_BUFFER_OFFSET_END(buf);
	element->next_timestamp = GST_BUFFER_PTS(buf) + GST_BUFFER_DURATION(buf);

	result = gst_pad_push(element->srcpad, buf);

	return result;
}


/*
 * ============================================================================
 *
 *                             GObject Overrides
 *
 * ============================================================================
 */


/*
 * properties
 */


enum property {
	ARG_SILENT = 1
};


static void set_property(GObject *object, enum property id, const GValue *value, GParamSpec *pspec)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(object);

	GST_OBJECT_LOCK(element);

	switch(id) {
	case ARG_SILENT:
		element->silent = g_value_get_boolean(value);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(element);
}


static void get_property(GObject *object, enum property id, GValue *value, GParamSpec *pspec)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(object);

	GST_OBJECT_LOCK(element);

	switch(id) {
	case ARG_SILENT:
		g_value_set_boolean(value, element->silent);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(element);
}


/*
 * finalize()
 */


static void finalize(GObject *object)
{
	GSTLALNoFakeDisconts *element = GSTLAL_NOFAKEDISCONTS(object);

	gst_object_unref(element->sinkpad);
	element->sinkpad = NULL;
	gst_object_unref(element->srcpad);
	element->srcpad = NULL;

	G_OBJECT_CLASS(gstlal_nofakedisconts_parent_class)->finalize(object);
}


/*
 * gstlal_nofakedisconts_class_init()
 */


static void gstlal_nofakedisconts_class_init(GSTLALNoFakeDiscontsClass *klass)
{
	GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
	GObjectClass *gobject_class = G_OBJECT_CLASS(klass);

	gst_element_class_set_details_simple(
		element_class,
		"Discontinuity flag fix",
		"Filter",
		"Fix incorrectly-set discontinuity flags",
		"Kipp Cannon <kipp.cannon@ligo.org>"
	);

	gobject_class->set_property = GST_DEBUG_FUNCPTR(set_property);
	gobject_class->get_property = GST_DEBUG_FUNCPTR(get_property);
	gobject_class->finalize = GST_DEBUG_FUNCPTR(finalize);
	gst_element_class_add_pad_template(
		element_class,
		gst_pad_template_new(
			"sink",
			GST_PAD_SINK,
			GST_PAD_ALWAYS,
			GST_CAPS_ANY
		)
	);
	gst_element_class_add_pad_template(
		element_class,
		gst_pad_template_new(
			"src",
			GST_PAD_SRC,
			GST_PAD_ALWAYS,
			GST_CAPS_ANY
		)
	);

	g_object_class_install_property(
		gobject_class,
		ARG_SILENT,
		g_param_spec_boolean(
			"silent",
			"Silent",
			"Don't print a message when alterning the flags in a buffer.",
			DEFAULT_SILENT,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);
}


/*
 * gstlal_nofakedisconts_init()
 */


static void gstlal_nofakedisconts_init(GSTLALNoFakeDisconts *element)
{
	GstPad *pad;

	gst_element_create_all_pads(GST_ELEMENT(element));

	/* configure (and ref) sink pad */
	pad = gst_element_get_static_pad(GST_ELEMENT(element), "sink");
	gst_pad_set_chain_function(pad, GST_DEBUG_FUNCPTR(chain));
	GST_PAD_SET_PROXY_CAPS(pad);
	GST_PAD_SET_PROXY_ALLOCATION(pad);
	GST_PAD_SET_PROXY_SCHEDULING(pad);
	element->sinkpad = pad;

	/* retrieve (and ref) src pad */
	pad = gst_element_get_static_pad(GST_ELEMENT(element), "src");
	GST_PAD_SET_PROXY_CAPS(pad);
	GST_PAD_SET_PROXY_ALLOCATION(pad);
	GST_PAD_SET_PROXY_SCHEDULING(pad);
	element->srcpad = pad;

	/* internal data */
	element->next_offset = GST_BUFFER_OFFSET_NONE;
	element->next_timestamp = GST_CLOCK_TIME_NONE;
	element->silent = DEFAULT_SILENT;
}
