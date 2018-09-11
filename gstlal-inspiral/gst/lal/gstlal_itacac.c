/*
 * An inspiral trigger, autocorrelation chisq, and coincidence (itacac) element
 *
 * Copyright (C) 2011 Chad Hanna, Kipp Cannon, 2018 Cody Messick, Alex Pace
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
 * stuff from C library, glib/gstreamer
 */


#include <glib.h>
#include <gst/gst.h>
#include <gst/base/gstadapter.h>
#include <gst/base/gstaggregator.h>
#include <gst/controller/controller.h>
#include <math.h>
#include <string.h>
#include <gsl/gsl_errno.h>
#include <gsl/gsl_blas.h>
#include <complex.h>

/*
 * our own stuff
 */

#include <gstlal/gstlal.h>
#include <gstlal/gstlal_debug.h>
#include <gstlal_itacac.h>
#include <gstlal/gstlal_peakfinder.h>
#include <gstlal/gstaudioadapter.h>
#include <gstlal/gstlal_tags.h>
#include <gstlal/gstlal_autocorrelation_chi2.h>
#include <gstlal_snglinspiral.h>

/*
 * ============================================================================
 *
 *                           GStreamer Boiler Plate
 *
 * ============================================================================
 */


#define GST_CAT_DEFAULT gstlal_itacac_debug
GST_DEBUG_CATEGORY_STATIC(GST_CAT_DEFAULT);

G_DEFINE_TYPE(
	GSTLALItacacPad,
	gstlal_itacac_pad,
	GST_TYPE_AGGREGATOR_PAD
);

G_DEFINE_TYPE_WITH_CODE(
	GSTLALItacac,
	gstlal_itacac,
	GST_TYPE_AGGREGATOR,
	GST_DEBUG_CATEGORY_INIT(GST_CAT_DEFAULT, "lal_itacac", 0, "lal_itacac debug category")
);

/* 
 * Static pad templates, needed to make instances of GstAggregatorPad
 */

#define CAPS \
	"audio/x-raw, " \
	"format = (string) { " GST_AUDIO_NE(Z64) ", " GST_AUDIO_NE(Z128) ", " GST_AUDIO_NE(F64) "}, "\
	"rate = " GST_AUDIO_RATE_RANGE ", " \
	"channels = " GST_AUDIO_CHANNELS_RANGE ", " \
	"layout = (string) interleaved, " \
	"channel-mask = (bitmask) 0"

static GstStaticPadTemplate src_templ = GST_STATIC_PAD_TEMPLATE(
	"src",
	GST_PAD_SRC,
	GST_PAD_ALWAYS,
	GST_STATIC_CAPS("application/x-lal-snglinspiral")
);

static GstStaticPadTemplate sink_templ = GST_STATIC_PAD_TEMPLATE(
	"sink%d",
	GST_PAD_SINK,
	GST_PAD_REQUEST,
	GST_STATIC_CAPS(CAPS)
);

#define DEFAULT_SNR_THRESH 5.5

/*
 * ========================================================================
 *
 *                             Utility Functions
 *
 * ========================================================================
 */

static unsigned autocorrelation_channels(const GSTLALItacacPad *itacacpad) {
	return gstlal_autocorrelation_chi2_autocorrelation_channels(itacacpad->autocorrelation_matrix);
}

static unsigned autocorrelation_length(GSTLALItacacPad *itacacpad) {
	// The autocorrelation length should be the same for all of the detectors, so we can just use the first
	return gstlal_autocorrelation_chi2_autocorrelation_length(itacacpad->autocorrelation_matrix);
}

static guint64 output_num_samps(GSTLALItacacPad *itacacpad) {
	return (guint64) itacacpad->n;
}

static guint64 output_num_bytes(GSTLALItacacPad *itacacpad) {
	return (guint64) output_num_samps(itacacpad) * itacacpad->adapter->unit_size;
}

static int reset_time_and_offset(GSTLALItacac *itacac) {
	itacac->next_output_offset = 0;
	itacac->next_output_timestamp = GST_CLOCK_TIME_NONE;
        return 0;
}


static guint gst_audioadapter_available_samples(GstAudioAdapter *adapter) {
        guint size;
        g_object_get(adapter, "size", &size, NULL);
        return size;
}

static void free_bank(GSTLALItacacPad *itacacpad) {
	g_free(itacacpad->bank_filename);
	itacacpad->bank_filename = NULL;
	free(itacacpad->bankarray);
	itacacpad->bankarray = NULL;
}

static void update_peak_info_from_autocorrelation_properties(GSTLALItacacPad *itacacpad) {
	// FIXME Need to make sure that itacac can run without autocorrelation matrix
	if (itacacpad->maxdata && itacacpad->tmp_maxdata && itacacpad->autocorrelation_matrix) {
		itacacpad->maxdata->pad = itacacpad->tmp_maxdata->pad = autocorrelation_length(itacacpad) / 2;
		if (itacacpad->snr_mat)
			free(itacacpad->snr_mat);
		if (itacacpad->tmp_snr_mat)
			free(itacacpad->tmp_snr_mat);

		itacacpad->snr_mat = calloc(autocorrelation_channels(itacacpad) * autocorrelation_length(itacacpad), itacacpad->maxdata->unit);
		itacacpad->tmp_snr_mat = calloc(autocorrelation_channels(itacacpad) * autocorrelation_length(itacacpad), itacacpad->tmp_maxdata->unit);
		itacacpad->snr_mat_array[0] = itacacpad->snr_mat;
		itacacpad->snr_mat_array[1] = itacacpad->tmp_snr_mat;

		//
		// Each row is one sample point of the snr time series with N
		// columns for N channels. Assumes proper packing to go from real to complex.
		// FIXME assumes single precision
		//
		itacacpad->snr_matrix_view = gsl_matrix_complex_float_view_array((float *) itacacpad->snr_mat, autocorrelation_length(itacacpad), autocorrelation_channels(itacacpad));
		itacacpad->tmp_snr_matrix_view = gsl_matrix_complex_float_view_array((float *) itacacpad->tmp_snr_mat, autocorrelation_length(itacacpad), autocorrelation_channels(itacacpad));
        }
}


/*
 * ============================================================================
 *
 *                                 Events
 *
 * ============================================================================
 */


static gboolean taglist_extract_string(GstObject *object, GstTagList *taglist, const char *tagname, gchar **dest)
{
	if(!gst_tag_list_get_string(taglist, tagname, dest)) {
		GST_WARNING_OBJECT(object, "unable to parse \"%s\" from %" GST_PTR_FORMAT, tagname, taglist);
		return FALSE;
	}
        return TRUE;
}



static gboolean setcaps(GstAggregator *agg, GstAggregatorPad *aggpad, GstEvent *event) {
	GSTLALItacac *itacac = GSTLAL_ITACAC(agg);
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(aggpad);
	GstCaps *caps;
	guint width = 0; 

	//
	// Update element metadata
	//
	gst_event_parse_caps(event, &caps);
	GstStructure *str = gst_caps_get_structure(caps, 0);
	const gchar *format = gst_structure_get_string(str, "format");
	gst_structure_get_int(str, "rate", &(itacacpad->rate));

	if(!strcmp(format, GST_AUDIO_NE(Z64))) {
		width = sizeof(float complex);
		itacacpad->peak_type = GSTLAL_PEAK_COMPLEX;
	} else if(!strcmp(format, GST_AUDIO_NE(Z128))) {
		width = sizeof(double complex);
		itacacpad->peak_type = GSTLAL_PEAK_DOUBLE_COMPLEX;
	} else
		GST_ERROR_OBJECT(itacac, "unsupported format %s", format);

	g_object_set(itacacpad->adapter, "unit-size", itacacpad->channels * width, NULL); 
	itacacpad->chi2 = calloc(itacacpad->channels, width);
	itacacpad->tmp_chi2 = calloc(itacacpad->channels, width);
	itacacpad->chi2_array[0] = itacacpad->chi2;
	itacacpad->chi2_array[1] = itacacpad->tmp_chi2;

	if (itacacpad->maxdata)
		gstlal_peak_state_free(itacacpad->maxdata);
	
	if (itacacpad->tmp_maxdata)
		gstlal_peak_state_free(itacacpad->tmp_maxdata);

	itacacpad->maxdata = gstlal_peak_state_new(itacacpad->channels, itacacpad->peak_type);
	itacacpad->tmp_maxdata = gstlal_peak_state_new(itacacpad->channels, itacacpad->peak_type);
	itacacpad->maxdata_array[0] = itacacpad->maxdata;
	itacacpad->maxdata_array[1] = itacacpad->tmp_maxdata;

	// This should be called any time the autocorrelation property is updated 
	update_peak_info_from_autocorrelation_properties(itacacpad);

	// The max size to copy from an adapter is the typical output size plus
	// the padding. we should never try to copy from an adapter with a
	// larger buffer than this
	itacacpad->data = malloc(output_num_bytes(itacacpad) + itacacpad->adapter->unit_size * itacacpad->maxdata->pad * 2);

	// Done

	return GST_AGGREGATOR_CLASS(gstlal_itacac_parent_class)->sink_event(agg, aggpad, event);


}

//static gboolean sink_event(GstPad *pad, GstObject *parent, GstEvent *event)
static gboolean sink_event(GstAggregator *agg, GstAggregatorPad *aggpad, GstEvent *event)
{
	// Right now no memory is allocated in the class instance structure for GSTLALItacacPads, so we dont need a custom finalize function
	// If anything is added to the class structure, we will need a custom finalize function that chains up to the AggregatorPad's finalize function
	GSTLALItacac *itacac = GSTLAL_ITACAC(agg);
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(aggpad);
	gboolean result = TRUE;

	GST_DEBUG_OBJECT(aggpad, "Got %s event on sink pad", GST_EVENT_TYPE_NAME (event));

	switch (GST_EVENT_TYPE(event)) {
		case GST_EVENT_CAPS:
		{
			return setcaps(agg, aggpad, event);
		}
		case GST_EVENT_TAG:
		{
			GstTagList *taglist;
			gchar *instrument, *channel_name;
			gst_event_parse_tag(event, &taglist);
			result = taglist_extract_string(GST_OBJECT(aggpad), taglist, GSTLAL_TAG_INSTRUMENT, &instrument);
			result &= taglist_extract_string(GST_OBJECT(aggpad), taglist, GSTLAL_TAG_CHANNEL_NAME, &channel_name);
			if(result) {
				GST_DEBUG_OBJECT(aggpad, "found tags \"%s\"=\"%s\", \"%s\"=\"%s\"", GSTLAL_TAG_INSTRUMENT, instrument, GSTLAL_TAG_CHANNEL_NAME, channel_name);
				g_free(itacacpad->instrument);
				itacacpad->instrument = instrument;
				g_free(itacacpad->channel_name);
				itacacpad->channel_name = channel_name;
				g_mutex_lock(&itacacpad->bank_lock);
				gstlal_set_channel_in_snglinspiral_array(itacacpad->bankarray, itacacpad->channels, itacacpad->channel_name);
				gstlal_set_instrument_in_snglinspiral_array(itacacpad->bankarray, itacacpad->channels, itacacpad->instrument);
				g_mutex_unlock(&itacacpad->bank_lock);
			}
                        break;

		}
		case GST_EVENT_EOS:
		{
			itacac->EOS = TRUE;
			break;
		}
		default:
			break;
	}
	if(!result) {
		gst_event_unref(event);
	} else {
		result = GST_AGGREGATOR_CLASS(gstlal_itacac_parent_class)->sink_event(agg, aggpad, event);
	}
	return result;
}

/*
 * ============================================================================
 *
 *                                 Properties
 *
 * ============================================================================
 */


enum padproperty {
	ARG_N = 1,
	ARG_SNR_THRESH,
	ARG_BANK_FILENAME,
	ARG_SIGMASQ,
	ARG_AUTOCORRELATION_MATRIX,
	ARG_AUTOCORRELATION_MASK
};

static void gstlal_itacac_pad_set_property(GObject *object, enum padproperty id, const GValue *value, GParamSpec *pspec)
{
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(object);

	GST_OBJECT_LOCK(itacacpad);

	switch(id) {
	case ARG_N:
		itacacpad->n = g_value_get_uint(value);
		break;

	case ARG_SNR_THRESH:
		itacacpad->snr_thresh = g_value_get_double(value);
		break;

	case ARG_BANK_FILENAME:
		g_mutex_lock(&itacacpad->bank_lock);
		itacacpad->bank_filename = g_value_dup_string(value);
		itacacpad->channels = gstlal_snglinspiral_array_from_file(itacacpad->bank_filename, &(itacacpad->bankarray));
		gstlal_set_min_offset_in_snglinspiral_array(itacacpad->bankarray, itacacpad->channels, &(itacacpad->difftime));
		if(itacacpad->instrument && itacacpad->channel_name) {
			gstlal_set_instrument_in_snglinspiral_array(itacacpad->bankarray, itacacpad->channels, itacacpad->instrument);
			gstlal_set_channel_in_snglinspiral_array(itacacpad->bankarray, itacacpad->channels, itacacpad->channel_name);
		}
		g_mutex_unlock(&itacacpad->bank_lock);
		break;

	case ARG_SIGMASQ:
		g_mutex_lock(&itacacpad->bank_lock);
		if (itacacpad->bankarray) {
			gint length;
			double *sigmasq = gstlal_doubles_from_g_value_array(g_value_get_boxed(value), NULL, &length);
			if((gint) itacacpad->channels != length)
				GST_ERROR_OBJECT(itacacpad, "vector length (%d) does not match number of templates (%d)", length, itacacpad->channels);
			else
				gstlal_set_sigmasq_in_snglinspiral_array(itacacpad->bankarray, length, sigmasq);
			g_free(sigmasq);
		} else
			GST_WARNING_OBJECT(itacacpad, "must set template bank before setting sigmasq");
		g_mutex_unlock(&itacacpad->bank_lock);
		break;


	case ARG_AUTOCORRELATION_MATRIX:
		g_mutex_lock(&itacacpad->bank_lock);

		if(itacacpad->autocorrelation_matrix)
			gsl_matrix_complex_free(itacacpad->autocorrelation_matrix);

		itacacpad->autocorrelation_matrix = gstlal_gsl_matrix_complex_from_g_value_array(g_value_get_boxed(value)); 

		// This should be called any time caps change too
		update_peak_info_from_autocorrelation_properties(itacacpad);

		//
		// induce norms to be recomputed
		//

		if(itacacpad->autocorrelation_norm) {
			gsl_vector_free(itacacpad->autocorrelation_norm);
			itacacpad->autocorrelation_norm = NULL;
		}

		g_mutex_unlock(&itacacpad->bank_lock);
		break;

        case ARG_AUTOCORRELATION_MASK: 
		g_mutex_lock(&itacacpad->bank_lock);

		if(itacacpad->autocorrelation_mask)
			gsl_matrix_int_free(itacacpad->autocorrelation_mask);

		itacacpad->autocorrelation_mask = gstlal_gsl_matrix_int_from_g_value_array(g_value_get_boxed(value));

		g_mutex_unlock(&itacacpad->bank_lock);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(itacacpad);
}


static void gstlal_itacac_pad_get_property(GObject *object, enum padproperty id, GValue *value, GParamSpec *pspec)
{
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(object);

	GST_OBJECT_LOCK(itacacpad);

	switch(id) {
	case ARG_N:
		g_value_set_uint(value, itacacpad->n);
		break;

	case ARG_SNR_THRESH:
		g_value_set_double(value, itacacpad->snr_thresh);
		break;

	case ARG_BANK_FILENAME:
		g_mutex_lock(&itacacpad->bank_lock);
		g_value_set_string(value, itacacpad->bank_filename);
		g_mutex_unlock(&itacacpad->bank_lock);
		break;

        case ARG_SIGMASQ:
		g_mutex_lock(&itacacpad->bank_lock);
		if(itacacpad->bankarray) {
			double sigmasq[itacacpad->channels];
			gint i;
			for(i = 0; i < (gint) itacacpad->channels; i++)
				sigmasq[i] = itacacpad->bankarray[i].sigmasq;
			g_value_take_boxed(value, gstlal_g_value_array_from_doubles(sigmasq, itacacpad->channels));
		} else {
			GST_WARNING_OBJECT(itacacpad, "no template bank");
			//g_value_take_boxed(value, g_value_array_new(0));
			// FIXME Is this right?
			g_value_take_boxed(value, g_array_sized_new(TRUE, TRUE, sizeof(double), 0));
		}
		g_mutex_unlock(&itacacpad->bank_lock);
		break;

	case ARG_AUTOCORRELATION_MATRIX:
		g_mutex_lock(&itacacpad->bank_lock);
		if(itacacpad->autocorrelation_matrix)
                        g_value_take_boxed(value, gstlal_g_value_array_from_gsl_matrix_complex(itacacpad->autocorrelation_matrix));
                else {
                        GST_WARNING_OBJECT(itacacpad, "no autocorrelation matrix");
			// FIXME g_value_array_new() is deprecated
                        //g_value_take_boxed(value, g_value_array_new(0)); 
			// FIXME Is this right?
			g_value_take_boxed(value, g_array_sized_new(TRUE, TRUE, sizeof(double), 0));
                        }
                g_mutex_unlock(&itacacpad->bank_lock);
                break;

	case ARG_AUTOCORRELATION_MASK:
		g_mutex_lock(&itacacpad->bank_lock);
		if(itacacpad->autocorrelation_mask)
			g_value_take_boxed(value, gstlal_g_value_array_from_gsl_matrix_int(itacacpad->autocorrelation_mask));
		else {
			GST_WARNING_OBJECT(itacacpad, "no autocorrelation mask");
			//g_value_take_boxed(value, g_value_array_new(0));
			// FIXME Is this right?
			g_value_take_boxed(value, g_array_sized_new(TRUE, TRUE, sizeof(double), 0));
		}
		g_mutex_unlock(&itacacpad->bank_lock);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(itacacpad);
}

/*
 * aggregate()
*/ 

static void update_state(GSTLALItacac *itacac, GstBuffer *srcbuf) {
	itacac->next_output_offset = GST_BUFFER_OFFSET_END(srcbuf);
	itacac->next_output_timestamp = GST_BUFFER_PTS(srcbuf) - itacac->difftime;
	itacac->next_output_timestamp += GST_BUFFER_DURATION(srcbuf);
}

//static void update_sink_state(GSTLALItacac *itacac


static GstFlowReturn push_buffer(GSTLALItacac *itacac, GstBuffer *srcbuf) {
	GstFlowReturn result = GST_FLOW_OK;
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(GST_ELEMENT(itacac)->sinkpads->data);

	update_state(itacac, srcbuf);

	GST_DEBUG_OBJECT(itacac, "pushing %" GST_BUFFER_BOUNDARIES_FORMAT, GST_BUFFER_BOUNDARIES_ARGS(srcbuf));

	result = gst_pad_push(GST_PAD((itacac->aggregator).srcpad), srcbuf);
	return result;
}

static GstFlowReturn push_gap(GSTLALItacac *itacac, guint samps) {
	GstFlowReturn result = GST_FLOW_OK;
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(GST_ELEMENT(itacac)->sinkpads->data);
	GstBuffer *srcbuf = gst_buffer_new();
	GST_BUFFER_FLAG_SET(srcbuf, GST_BUFFER_FLAG_GAP);

	GST_BUFFER_OFFSET(srcbuf) = itacac->next_output_offset;
	GST_BUFFER_OFFSET_END(srcbuf) = itacac->next_output_offset + samps;
	GST_BUFFER_PTS(srcbuf) = itacac->next_output_timestamp + itacac->difftime;
	GST_BUFFER_DURATION(srcbuf) = (GstClockTime) gst_util_uint64_scale_int_round(GST_SECOND, samps, itacac->rate);
	GST_BUFFER_DTS(srcbuf) = GST_BUFFER_PTS(srcbuf);

	result = push_buffer(itacac, srcbuf);

	return result;

}

static void check_sinkpad_compatibility(GSTLALItacac *itacac) {
	GstElement *element = GST_ELEMENT(itacac);
	GSTLALItacacPad *itacacpad;
	GList *padlist;

	// Ensure all of the pads have the same channels and rate, and set them on itacac for easy access
	for(padlist = element->sinkpads; padlist !=NULL; padlist = padlist->next) {
		itacacpad = GSTLAL_ITACAC_PAD(padlist->data);
		// FIXME Should gst_object_sync_values be called here too?
		if(padlist == element->sinkpads){
			itacac->channels = itacacpad->channels;
			itacac->rate = itacacpad->rate;
			itacac->difftime = itacacpad->difftime;
			itacac->peak_type = itacacpad->peak_type;
		} else {
			g_assert(itacac->channels == itacacpad->channels);
			g_assert(itacac->rate == itacacpad->rate);
			g_assert(itacac->difftime == itacacpad->difftime);
			g_assert(itacac->peak_type == itacacpad->peak_type);
		}

	}
}

static void generate_trigger(GSTLALItacac *itacac, GSTLALItacacPad *itacacpad, guint copysamps, guint peak_finding_length, guint processed_samples, gboolean numerous_peaks_in_window) {
	gsl_error_handler_t *old_gsl_error_handler;
	union {
		float complex * as_complex;
		double complex * as_double_complex;
		void * as_void;
	} dataptr;

	struct gstlal_peak_state *this_maxdata;
	void *this_snr_mat;
	void *this_chi2;
	guint channel;

	// Need to use our tmp_chi2 and tmp_maxdata struct and its corresponding snr_mat if we've already found a peak in this window
	if(numerous_peaks_in_window) {
		this_maxdata = itacacpad->maxdata_array[1];
		this_snr_mat = itacacpad->snr_mat_array[1];
		this_chi2 = itacacpad->chi2_array[1];
	} else {
		this_maxdata = itacacpad->maxdata_array[0];
		this_snr_mat = itacacpad->snr_mat_array[0];
		this_chi2 = itacacpad->chi2_array[0];
	}

	// Update the snr threshold
	this_maxdata->thresh = itacacpad->snr_thresh;
	
	// call the peak finding library on a buffer from the adapter if no events are found the result will be a GAP
	gst_audioadapter_copy_samples(itacacpad->adapter, itacacpad->data, copysamps, NULL, NULL);

	// AEP- 180417 Turning XLAL Errors off
	old_gsl_error_handler=gsl_set_error_handler_off();

        // put the data pointer one pad length in 
        if (itacac->peak_type == GSTLAL_PEAK_COMPLEX) {
                dataptr.as_complex = ((float complex *) itacacpad->data) + this_maxdata->pad * this_maxdata->channels;
                // Find the peak 
                gstlal_float_complex_peak_over_window_interp(this_maxdata, dataptr.as_complex, peak_finding_length);
                }
        else if (itacac->peak_type == GSTLAL_PEAK_DOUBLE_COMPLEX) {
                dataptr.as_double_complex = ((double complex *) itacacpad->data) + this_maxdata->pad * this_maxdata->channels;
                // Find the peak 
                gstlal_double_complex_peak_over_window_interp(this_maxdata, dataptr.as_double_complex, peak_finding_length);
                }
        else
                g_assert_not_reached();

        // AEP- 180417 Turning XLAL Errors back on
        gsl_set_error_handler(old_gsl_error_handler);

	// Compute \chi^2 values if we can
	if(itacacpad->autocorrelation_matrix) {
		// Compute the chisq norm if it doesn't exist
		if(!itacacpad->autocorrelation_norm)
			itacacpad->autocorrelation_norm = gstlal_autocorrelation_chi2_compute_norms(itacacpad->autocorrelation_matrix, NULL);

		g_assert(autocorrelation_length(itacacpad) & 1);  // must be odd 

		if(itacac->peak_type == GSTLAL_PEAK_DOUBLE_COMPLEX) {
			/* extract data around peak for chisq calculation */
			gstlal_double_complex_series_around_peak(this_maxdata, dataptr.as_double_complex, (double complex *) this_snr_mat, this_maxdata->pad);
			gstlal_autocorrelation_chi2((double *) this_chi2, (double complex *) this_snr_mat, autocorrelation_length(itacacpad), -((int) autocorrelation_length(itacacpad)) / 2, 0.0, itacacpad->autocorrelation_matrix, itacacpad->autocorrelation_mask, itacacpad->autocorrelation_norm);

		} else if (itacac->peak_type == GSTLAL_PEAK_COMPLEX) {
			/* extract data around peak for chisq calculation */
			gstlal_float_complex_series_around_peak(this_maxdata, dataptr.as_complex, (float complex *) this_snr_mat, this_maxdata->pad);
			gstlal_autocorrelation_chi2_float((float *) this_chi2, (float complex *) this_snr_mat, autocorrelation_length(itacacpad), -((int) autocorrelation_length(itacacpad)) / 2, 0.0, itacacpad->autocorrelation_matrix, itacacpad->autocorrelation_mask, itacacpad->autocorrelation_norm);
		} else
			g_assert_not_reached();
	} 

	// Adjust the location of the peak by the number of samples processed in this window before this function call
	if(processed_samples > 0) {
		if(itacac->peak_type == GSTLAL_PEAK_DOUBLE_COMPLEX) {
			for(channel=0; channel < this_maxdata->channels; channel++) {
				if(cabs( (double complex) (this_maxdata->values).as_double_complex[channel]) > 0) {
					this_maxdata->samples[channel] += processed_samples;
					this_maxdata->interpsamples[channel] += (double) processed_samples;
				}
			}
		} else {
			for(channel=0; channel < this_maxdata->channels; channel++) {
				if(cabs( (double complex) (this_maxdata->values).as_float_complex[channel]) > 0) {
					this_maxdata->samples[channel] += processed_samples;
					this_maxdata->interpsamples[channel] += (double) processed_samples;
				}
			}
		}
	}

	// Combine with previous peaks found if any
	if(numerous_peaks_in_window) {
		// replace an original peak with a second peak, we need to...
		// Replace maxdata->interpvalues.as_float_complex, maxdata->interpvalues.as_double_complex etc with whichever of the two is larger
		// // Do same as above, but with maxdata->values instead of maxdata->interpvalues
		// // Make sure to replace samples and interpsamples too
		gsl_vector_complex_float_view old_snr_vector_view, new_snr_vector_view;
		double old_snr, new_snr;
		if(itacac->peak_type == GSTLAL_PEAK_DOUBLE_COMPLEX) {
			double *old_chi2 = (double *) itacacpad->chi2;
			double *new_chi2 = (double *) itacacpad->tmp_chi2;
			for(channel=0; channel < itacacpad->maxdata->channels; channel++) {
				// Possible cases
				// itacacpad->maxdata has a peak but itacacpad->tmp_maxdata does not <--No change required
				// itacacpad->tmp_maxdata has a peak but itacacpad->maxdata does not <--Swap out peaks
				// Both have peaks and itacacpad->maxdata's is higher <--No change required
				// Both have peaks and itacacpad->tmp_maxdata's is higher <--Swap out peaks
				old_snr = cabs( (double complex) (itacacpad->maxdata->interpvalues).as_double_complex[channel]);
				new_snr = cabs( (double complex) (itacacpad->tmp_maxdata->interpvalues).as_double_complex[channel]);

				if(new_snr > old_snr) {
					// The previous peak found was larger than the current peak. If there was a peak before but not now, increment itacacpad->maxdata's num_events
					if(old_snr == 0)
						// FIXME confirm that this isnt affected by floating point error
						itacacpad->maxdata->num_events++;

					(itacacpad->maxdata->values).as_double_complex[channel] = (itacacpad->tmp_maxdata->values).as_double_complex[channel];
					(itacacpad->maxdata->interpvalues).as_double_complex[channel] = (itacacpad->tmp_maxdata->interpvalues).as_double_complex[channel];
					itacacpad->maxdata->samples[channel] = itacacpad->tmp_maxdata->samples[channel];
					itacacpad->maxdata->interpsamples[channel] = itacacpad->tmp_maxdata->interpsamples[channel];
					old_chi2[channel] = new_chi2[channel];

					if(itacacpad->autocorrelation_matrix) {
						// Replace the snr time series around the peak with the new one
						old_snr_vector_view = gsl_matrix_complex_float_column(&(itacacpad->snr_matrix_view.matrix), channel);
						new_snr_vector_view = gsl_matrix_complex_float_column(&(itacacpad->tmp_snr_matrix_view.matrix), channel);
						old_snr_vector_view.vector = new_snr_vector_view.vector;
					}
				}
			}
		} else {
			float *old_chi2 = (float *) itacacpad->chi2;
			float *new_chi2 = (float *) itacacpad->tmp_chi2;
			for(channel=0; channel < itacacpad->maxdata->channels; channel++) {
				// Possible cases
				// itacacpad->maxdata has a peak but itacacpad->tmp_maxdata does not <--No change required
				// itacacpad->tmp_maxdata has a peak but itacacpad->maxdata does not <--Swap out peaks
				// Both have peaks and itacacpad->maxdata's is higher <--No change required 
				// Both have peaks and itacacpad->tmp_maxdata's is higher <--Swap out peaks
				old_snr = cabs( (double complex) (itacacpad->maxdata->interpvalues).as_float_complex[channel]);
				new_snr = cabs( (double complex) (itacacpad->tmp_maxdata->interpvalues).as_float_complex[channel]);
				if(new_snr > old_snr) {
					// The previous peak found was larger than the current peak. If there was a peak before but not now, increment itacacpad->maxdata's num_events
					if(old_snr == 0)
						// FIXME confirm that this isnt affected by floating point error
						itacacpad->maxdata->num_events++;

					(itacacpad->maxdata->values).as_float_complex[channel] = (itacacpad->tmp_maxdata->values).as_float_complex[channel];
					(itacacpad->maxdata->interpvalues).as_float_complex[channel] = (itacacpad->tmp_maxdata->interpvalues).as_float_complex[channel];
					itacacpad->maxdata->samples[channel] = itacacpad->tmp_maxdata->samples[channel];
					itacacpad->maxdata->interpsamples[channel] = itacacpad->tmp_maxdata->interpsamples[channel];
					old_chi2[channel] = new_chi2[channel];

					if(itacacpad->autocorrelation_matrix) {
						// Replace the snr time series around the peak with the new one
						old_snr_vector_view = gsl_matrix_complex_float_column(&(itacacpad->snr_matrix_view.matrix), channel);
						new_snr_vector_view = gsl_matrix_complex_float_column(&(itacacpad->tmp_snr_matrix_view.matrix), channel);
						old_snr_vector_view.vector = new_snr_vector_view.vector;
					}
				}
			}
		}
	}

}

static GstFlowReturn process(GSTLALItacac *itacac) {
	// Iterate through audioadapters and generate triggers
	// FIXME Needs to push a gap for the padding of the first buffer with nongapsamps

	
	GstElement *element = GST_ELEMENT(itacac);
	guint outsamps, nongapsamps, copysamps, samples_left_in_window, previous_samples_left_in_window;
	gboolean triggers;
	guint gapsamps = 0;
	GstFlowReturn result = GST_FLOW_OK;
	GList *padlist;
	GSTLALItacacPad *itacacpad;
	GstBuffer *srcbuf = NULL;
	guint availablesamps;

	// Make sure we have enough samples to produce a trigger
	// All of the sinkpads should have the same number of samples, so we'll just check the first one FIXME this is probably not true
	// FIXME Currently assumes every pad has the same n
	itacacpad = GSTLAL_ITACAC_PAD(element->sinkpads->data);
	if(gst_audioadapter_available_samples( itacacpad->adapter ) <= itacacpad->n + 2*itacacpad->maxdata->pad && !itacac->EOS)
		return result;


	for(padlist = element->sinkpads; padlist != NULL; padlist = padlist->next) {
		itacacpad = GSTLAL_ITACAC_PAD(padlist->data);

		samples_left_in_window = itacacpad->n;
		triggers = FALSE;

		// FIXME Currently assumes n is the same for all detectors
		//while( samples_left_in_window > 0 && ( gst_audioadapter_available_samples(itacacpad->adapter) > samples_left_in_window + 2 * itacacpad->maxdata->pad || (itacac->EOS && gst_audioadapter_available_samples(itacacpad->adapter)) ) ) {
		while( samples_left_in_window > 0 && ( !itacac->EOS || (itacac->EOS && gst_audioadapter_available_samples(itacacpad->adapter)) ) ) {

			// Check how many gap samples there are until a nongap
			// or vice versa, depending on which comes first
			previous_samples_left_in_window = samples_left_in_window;
			gapsamps = gst_audioadapter_head_gap_length(itacacpad->adapter);
			nongapsamps = gst_audioadapter_head_nongap_length(itacacpad->adapter);
			availablesamps = gst_audioadapter_available_samples(itacacpad->adapter);

			// NOTE Remember that if you didnt just come off a gap, you should always have a pad worth of nongap samples that have been processed already

			// Check if the samples are gap, and flush up to samples_left_in_window of them if so
			if(gapsamps > 0) {
				itacacpad->last_gap = TRUE; // FIXME This will not work for case where each itacac has multiple sinkpads
				outsamps = gapsamps > samples_left_in_window ? samples_left_in_window : gapsamps;
				gst_audioadapter_flush_samples(itacacpad->adapter, outsamps);
				samples_left_in_window -= outsamps;
			}
			// Check if we have enough nongap samples to compute chisq for a potential trigger, and if not, check if we should flush the samples or 
			// if theres a possibility we could get more nongapsamples in the future
			else if(nongapsamps <= 2 * itacacpad->maxdata->pad) {
				if(samples_left_in_window >= itacacpad->maxdata->pad) {
					// We are guarenteed to have at least one sample more than a pad worth of samples past the end of the 
					// trigger window, thus we know there must be a gap sample after these, and can ditch them, though we 
					// need to make sure we aren't flushing any samples from the next trigger window
					// The only time adjust_window != 0 is if you're at the beginning of the window
					// FIXME If you're at the beginning of the window, where adjust_window can be nonzero, you will NEVER have more nongaps then samples_left_in_window, let alone samples_left_in_window + adjust_window. Could this be the bug?
					//outsamps = nongapsamps > samples_left_in_window + itacacpad->adjust_window ? samples_left_in_window + itacacpad->adjust_window: nongapsamps;
					g_assert(availablesamps > nongapsamps || itacac->EOS);
					outsamps = nongapsamps > samples_left_in_window ? samples_left_in_window : nongapsamps;
					gst_audioadapter_flush_samples(itacacpad->adapter, outsamps);

					if(!itacacpad->last_gap && itacacpad->adjust_window == 0 && samples_left_in_window == itacacpad->n) {
						// We are at the beginning of the window, and did not just come off a gap, thus the first pad 
						// worth of samples we flushed came from the previous window
						samples_left_in_window -= outsamps - itacacpad->maxdata->pad;
					} else 
						samples_left_in_window -= outsamps - itacacpad->adjust_window;

					if(itacacpad->adjust_window > 0)
						itacacpad->adjust_window = 0;

					//itacacpad->last_gap = TRUE; // FIXME This will not work for case where each itacac has multiple sinkpads
				} else if(nongapsamps == availablesamps && !itacac->EOS) {
					// We have reached the end of available samples, thus there could still be enough nongaps in the next 
					// window for a trigger, so we want to leave a pad worth of samples at the end of this window
					// FIXME this next line is assuming you have enough nongaps to fit into the next window, but it might just be a couple
					g_assert(nongapsamps > samples_left_in_window);
					itacacpad->adjust_window = samples_left_in_window;

					samples_left_in_window = 0;
					itacacpad->last_gap = FALSE;
				} else {
					// We know there is a gap after these, so we can flush these up to the edge of the window
					g_assert(nongapsamps < availablesamps || itacac->EOS);
					outsamps = nongapsamps >= samples_left_in_window ? samples_left_in_window : nongapsamps;
					gst_audioadapter_flush_samples(itacacpad->adapter, outsamps);
					samples_left_in_window -= outsamps;
				}
			}
			// Not enough samples left in the window to produce a trigger or possibly even fill up a pad for a trigger in the next window
			else if(samples_left_in_window <= itacacpad->maxdata->pad) {
				// Need to just zero out samples_left_in_window and set itacacpad->adjust_window for next iteration
				if(samples_left_in_window < itacacpad->maxdata->pad)
					itacacpad->adjust_window = samples_left_in_window;

				samples_left_in_window = 0;
				itacacpad->last_gap = FALSE;
			}
			// Previous window had fewer than maxdata->pad samples, which needs to be accounted for when generating a trigger and flushing samples.
			// This conditional will only ever return TRUE at the beginning of a window since itacacpad->adjust_window is only set to nonzero values
			// at the end of a window
			else if(itacacpad->adjust_window > 0) {
				// This should only ever happen at the beginning of a window, so we use itacacpad->n instead of samples_left_in_window for conditionals
				g_assert(samples_left_in_window == itacacpad->n);
				g_assert(itacacpad->last_gap == FALSE);
				copysamps = nongapsamps >= itacacpad->n + itacacpad->adjust_window + itacacpad->maxdata->pad ? itacacpad->n + itacacpad->adjust_window + itacacpad->maxdata->pad : nongapsamps;
				if(nongapsamps >= itacacpad->n + itacacpad->adjust_window + itacacpad->maxdata->pad) {
					// We have enough nongaps to cover this entire trigger window and a pad worth of samples in the next trigger window
					// We want to copy all of the samples up to a pad past the end of the window, and we want to flush 
					// all of the samples up until a pad worth of samples before the end of the window (leaving samples for a pad in the next window)
					// We want the peak finding length to be the length from the first sample after a pad worth of samples to the last sample in the window.
					// copysamps = itacacpad->n + itacacpad->adjust_window + itacacpad->maxdata->pad
					// outsamps = itacacpad->n + itacacpad->adjust_window - itacacpad->maxdata->pad
					// peak_finding_length = itacacpad->n - itacacpad->maxdata->pad + itacacpad->adjust_window = outsamps
					outsamps = itacacpad->n + itacacpad->adjust_window - itacacpad->maxdata->pad;
					generate_trigger(itacac, itacacpad, copysamps, outsamps, 0, triggers);
					itacacpad->last_gap = FALSE;
				} else if(nongapsamps >= itacacpad->n + itacacpad->adjust_window) {
					// We have enough nongaps to cover this entire trigger window, but not cover a full pad worth of samples in the next window
					// Because we are gaurenteed to have at least a pad worth of samples after this window, we know these samples preceed a gap
					// We want to copy all of the nongap samples, and we want to flush all of the samples up until the end of the current window
					// we want the peak finding length to be from the first sample after a pad worth of samples to the last sample that preceeds a pad worth of samples
					// copysamps = nongapsamps
					// outsamps = itacacpad->n + itacacpad->adjust_window
					// peak_finding_length = itacacpad->n + itacacpad->adjust_window - 2 * itacacpad->maxdata->pad = outsamps - 2 * itacacpad->maxdata->pad
					g_assert(availablesamps > nongapsamps);
					outsamps = itacacpad->n + itacacpad->adjust_window;
					generate_trigger(itacac, itacacpad, copysamps, outsamps - 2 * itacacpad->maxdata->pad, 0, triggers);
					itacacpad->last_gap = TRUE;
				} else {
					// There's a gap in the middle of this window or we've hit EOS
					// We want to copy and flush all of the samples up to the gap
					// We want the peak finding length to be the length from the first sample
					// after a pad worth of samples to the last sample that preceeds a pad worth of samples
					// copysamps = outsamps = nongapsamps
					// peak_finding_length = nongapsamps - 2*itacacpad->maxdata->pad
					g_assert(availablesamps > nongapsamps || itacac->EOS);
					outsamps = copysamps = nongapsamps;
					generate_trigger(itacac, itacacpad, copysamps, outsamps - 2*itacacpad->maxdata->pad, 0, triggers);
					//itacacpad->last_gap = TRUE;
				}
				gst_audioadapter_flush_samples(itacacpad->adapter, outsamps);
				// FIXME This can put in the conditionals with outsamps and generate_trigger once everything is working
				if(nongapsamps >= itacacpad->n + itacacpad->adjust_window) {
					samples_left_in_window = 0;
					//itacacpad->last_gap = FALSE;
				} else {
					samples_left_in_window -= (outsamps - itacacpad->adjust_window);
				}
				triggers = TRUE;
				itacacpad->adjust_window = 0;
			}
			// If we've made it this far, we have enough nongap samples to generate a trigger
			else {

				// Possible scenarios
				//
				// REMEMBER that last_gap == FALSE means you're definitely at the beginning of the window, and we have a pad worth of samples 
				// from before this window starts (though the negation is not true)
				//
				if(!itacacpad->last_gap) {
					// last_gap == FALSE and nongaps >= samples_left_in_window + 2*pad
					// Have a pad worth of samples before this window and after this window
					// want to copy samples_left_in_window + 2* pad
					// Want to flush up to a pad worth of samples before the next window
					// Want peak finding length of samples_left_in_window
					// samples_left_in_window will be zero after this
					if(nongapsamps >= samples_left_in_window + 2*itacacpad->maxdata->pad) {
						copysamps = samples_left_in_window + 2*itacacpad->maxdata->pad;
						outsamps = samples_left_in_window;
						generate_trigger(itacac, itacacpad, copysamps, outsamps, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window = 0;
					}
					// last_gap == FALSE and nongaps < samples_left_in_window + 2*pad but nongaps >= samples_left_in_window + pad
					// this means you do not have a full pad worth of samples in the next window, and since we always guarenteed to get at least 
					// a pad full of samples after the window boundary, we know there's a gap there, so we can flush samples up to the window boundary.
					// In this case we want to copy all the nongaps we have
					// We want outsamps to go to the window boundary
					// The peak findiong length will be nongaps - 2*pad
					// samples_left_in_window will be zero after this
					else if(nongapsamps >= samples_left_in_window + itacacpad->maxdata->pad) {
						g_assert(availablesamps > nongapsamps || itacac->EOS);
						copysamps = nongapsamps;
						outsamps = samples_left_in_window + itacacpad->maxdata->pad;
						generate_trigger(itacac, itacacpad, copysamps, copysamps - 2*itacacpad->maxdata->pad, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window = 0;
						itacacpad->last_gap = TRUE;
					}
					// last_gap == FALSE and nongaps < samples_left_in_window + pad 
					// This means there is a gap somewhere in this trigger window, so we want to copy and flush up to that gap
					// Peak finding length in this case will be nongaps - 2*pad
					// samples_left_in_window -= (nongaps - pad)
					// Note that nothing changes if nongaps < samples_left_in_window
					else {
						copysamps = outsamps = nongapsamps;
						generate_trigger(itacac, itacacpad, copysamps, outsamps - 2*itacacpad->maxdata->pad, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window -= nongapsamps - itacacpad->maxdata->pad;
						//itacacpad->last_gap = TRUE;
					}
				} else {
					// last_gap == TRUE and nongaps >= samples_left_in_window + pad
					// this means we have enough samples in the next window to use for padding
					// we already know (from earlier in the if else if chain) that samples_left_in_window > 2pad <-------what if this wasnt true?
					// want to copy all samples up to a pad past the window boundary
					// want to flush all samples up to pad before the window boundary
					// want peak finding length to go from a pad into the nongapsamps to the end of the window, so samples_left_in_window - pad
					// samples_left_in_window will be zero after this
					if(nongapsamps >= samples_left_in_window + itacacpad->maxdata->pad) {
						copysamps = samples_left_in_window + itacacpad->maxdata->pad;
						outsamps = samples_left_in_window - itacacpad->maxdata->pad;
						generate_trigger(itacac, itacacpad, copysamps, outsamps, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window = 0;
						itacacpad->last_gap = FALSE;
					}
					// last_gap == TRUE and nongaps < samples_left_in_window + pad but nongaps >= samples_left_in_window
					// We dont have enough samples in the next window for padding the final sample in this window
					// We are guarenteed to have samples out to at least a pad past the window boundary (assuming we havent hit EOS), 
					// thus we know a gap is after these nongaps. So we want want to copy all of the nongaps, and flush them up to the window boundary
					// want peak finding length to go from a pad into the nongapsamps to a pad before the end of its, so nongapsamps - 2*pad
					// samples_left_in_window will be zero after this
					else if(nongapsamps >= samples_left_in_window) {
						g_assert(availablesamps > nongapsamps || itacac->EOS);
						copysamps = nongapsamps;
						outsamps = samples_left_in_window;
						generate_trigger(itacac, itacacpad, copysamps, copysamps - 2*itacacpad->maxdata->pad, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window = 0;
						itacacpad->last_gap = TRUE;
					}
					// last_gap == TRUE and nongaps < samples_left_in_window
					// These nongaps are sandwiched between two gaps
					// want to copy and flush all the nongaps
					// peak finding length will nongaps - 2*pad
					// samples_left_in_window -= nongaps
					else {
						copysamps = outsamps = nongapsamps;
						generate_trigger(itacac, itacacpad, copysamps, outsamps - 2*itacacpad->maxdata->pad, itacacpad->n - samples_left_in_window, triggers);
						samples_left_in_window -= nongapsamps;
						itacacpad->last_gap = FALSE;
					}
				}

				// Check if we have enough samples to compute chisq for every potential trigger, otherwise we'll need to knock padding off of whats available
				//copysamps = nongapsamps > (samples_left_in_window + 2 * itacacpad->maxdata->pad) ? samples_left_in_window + 2*itacacpad->maxdata->pad : nongapsamps;

				//
				//copysamps = nongapsamps > (samples_left_in_window + 2 * itacacpad->maxdata->pad) ? samples_left_in_window + 2*itacacpad->maxdata->pad : nongapsamps;
				//outsamps = copysamps == nongapsamps ? copysamps - 2 * itacacpad->maxdata->pad : samples_left_in_window;
				//generate_trigger(itacac, itacacpad, copysamps, outsamps, itacacpad->n - samples_left_in_window, triggers);

				gst_audioadapter_flush_samples(itacacpad->adapter, outsamps);
				//samples_left_in_window -= outsamps;
				triggers = TRUE;
			}
		}

		// FIXME Assumes n is the same for all detectors
		if(triggers && itacacpad->autocorrelation_matrix) {
			if(!srcbuf) {
				srcbuf = gstlal_snglinspiral_new_buffer_from_peak(itacacpad->maxdata, itacacpad->bankarray, GST_PAD((itacac->aggregator).srcpad), itacac->next_output_offset, itacacpad->n, itacac->next_output_timestamp, itacac->rate, itacacpad->chi2, &(itacacpad->snr_matrix_view), itacac->difftime);
			} else {
				gstlal_snglinspiral_append_peak_to_buffer(srcbuf, itacacpad->maxdata, itacacpad->bankarray, GST_PAD((itacac->aggregator).srcpad), itacac->next_output_offset, itacacpad->n, itacac->next_output_timestamp, itacac->rate, itacacpad->chi2, &(itacacpad->snr_matrix_view));
			}
		} else if(triggers) {
			if(!srcbuf)
				srcbuf = gstlal_snglinspiral_new_buffer_from_peak(itacacpad->maxdata, itacacpad->bankarray, GST_PAD((itacac->aggregator).srcpad), itacac->next_output_offset, itacacpad->n, itacac->next_output_timestamp, itacac->rate, itacacpad->chi2, NULL, itacac->difftime);
			else
				gstlal_snglinspiral_append_peak_to_buffer(srcbuf, itacacpad->maxdata, itacacpad->bankarray, GST_PAD((itacac->aggregator).srcpad), itacac->next_output_offset, itacacpad->n, itacac->next_output_timestamp, itacac->rate, itacacpad->chi2, NULL);
		}

	}


	if(!itacac->EOS) {
		if(srcbuf)
			result = push_buffer(itacac, srcbuf);
		else
			result = push_gap(itacac, itacacpad->n);
	} else {
		guint max_num_samps_left_in_any_pad = 0;
		guint available_samps;
		for(padlist=element->sinkpads; padlist != NULL; padlist = padlist->next) {
			itacacpad = GSTLAL_ITACAC_PAD(padlist->data);
			available_samps = gst_audioadapter_available_samples(itacacpad->adapter);
			max_num_samps_left_in_any_pad = available_samps > max_num_samps_left_in_any_pad ? available_samps : max_num_samps_left_in_any_pad;
		}

		// If there aren't any samples left to process, then we're ready to return GST_FLOW_EOS
		if(max_num_samps_left_in_any_pad > 0)
			result = process(itacac);
		else 
			result = GST_FLOW_EOS;
	}

	

	return result;

	
}

static GstFlowReturn aggregate(GstAggregator *aggregator, gboolean timeout)
{
	GSTLALItacac *itacac = GSTLAL_ITACAC(aggregator);
	GSTLALItacacPad *itacacpad;
	GList *padlist;
	GstBuffer *sinkbuf;
	GstFlowReturn result;


	// Make sure the pads caps are compatible with each other if we're just starting
	if(itacac->rate == 0)
		check_sinkpad_compatibility(itacac);

	if(itacac->EOS) {
		result = process(itacac);
		return result;
	}
		

	// FIXME need to confirm the aggregator does enough checks that the
	// checks itac does are unncessary
	for(padlist = GST_ELEMENT(aggregator)->sinkpads; padlist != NULL; padlist = padlist->next) {
		// Get the buffer from the pad we're looking at and assert it
		// has a valid timestamp
		itacacpad = GSTLAL_ITACAC_PAD(padlist->data);
		sinkbuf = gst_aggregator_pad_pop_buffer(GST_AGGREGATOR_PAD(itacacpad));
		g_assert(GST_BUFFER_PTS_IS_VALID(sinkbuf));

		// FIXME Is this necessary/did I understand what this does correctly?
		// Sync up the properties that may have changed, do this before
		// accessing any of the pad's properties
		gst_object_sync_values(GST_OBJECT(itacacpad), GST_BUFFER_PTS(sinkbuf));

		if(!GST_BUFFER_PTS_IS_VALID(sinkbuf) || !GST_BUFFER_DURATION_IS_VALID(sinkbuf) || !GST_BUFFER_OFFSET_IS_VALID(sinkbuf) || !GST_BUFFER_OFFSET_END_IS_VALID(sinkbuf)) {
			gst_buffer_unref(sinkbuf);
			GST_ERROR_OBJECT(GST_ELEMENT(aggregator), "error in input stream: buffer has invalid timestamp and/or offset");
			result = GST_FLOW_ERROR;
			return result;
        }

		// Check for instrument and channel name tags
		if(!itacacpad->instrument || !itacacpad->channel_name) {
			GST_ELEMENT_ERROR(itacacpad, STREAM, FAILED, ("missing or invalid tags"), ("instrument and/or channel name not known (stream's tags must provide this information)"));
			result = GST_FLOW_ERROR;
			return result;
		}

		if(!itacacpad->bankarray) {
			GST_ELEMENT_ERROR(itacacpad, STREAM, FAILED, ("missing bank file"), ("must have a valid template bank to create events"));
			result = GST_FLOW_ERROR;
			return result;
		}

		// FIXME if we were more careful we wouldn't lose so much data around disconts
		// FIXME I don't think this logic works for itacac, it came from itac, need to think carefully about what to do around disconts
		if (GST_BUFFER_FLAG_IS_SET(sinkbuf, GST_BUFFER_FLAG_DISCONT)) {
			reset_time_and_offset(itacac);
			gst_audioadapter_clear(itacacpad->adapter);
		}

		// If we dont have a valid first timestamp yet take this one
		// The aggregator keeps everything in sync, so it should be
		// fine to just take this one
		if(itacac->next_output_timestamp == GST_CLOCK_TIME_NONE) {
			itacac->next_output_timestamp = GST_BUFFER_PTS(sinkbuf);
		}

		// put the incoming buffer into an adapter, handles gaps 
		// FIXME the aggregator does have some logic to deal with gaps,
		// should see if we can use some built-in freatures of the
		// aggregator instead of the audioadapter
		gst_audioadapter_push(itacacpad->adapter, sinkbuf);
	}

	result = process(itacac);

	return result;
}


/*
 * ============================================================================
 *
 *                                Type Support FIXME Is this appropriately named?
 *
 * ============================================================================
 */


/*
 * Instance finalize function.  See ???
 */

static void gstlal_itacac_pad_dispose(GObject *object)
{
	GSTLALItacacPad *itacacpad = GSTLAL_ITACAC_PAD(object);

	gst_audioadapter_clear(itacacpad->adapter);
	g_object_unref(itacacpad->adapter);

	if (itacacpad->bankarray)
		free_bank(itacacpad);

	if (itacacpad->instrument){
		free(itacacpad->instrument);
		itacacpad->instrument = NULL;
	}

	if(itacacpad->channel_name){
		free(itacacpad->channel_name);
		itacacpad->channel_name = NULL;
	}

	if(itacacpad->maxdata) {
		gstlal_peak_state_free(itacacpad->maxdata);
		itacacpad->maxdata = NULL;
	}

	if(itacacpad->tmp_maxdata) {
		gstlal_peak_state_free(itacacpad->tmp_maxdata);
		itacacpad->tmp_maxdata = NULL;
	}

	if(itacacpad->data) {
		free(itacacpad->data);
		itacacpad->data = NULL;
	}

	if(itacacpad->snr_mat) {
		//memset(itacacpad->snr_mat, 0, sizeof(*itacacpad->snr_mat)); <-- Remove after confirming its not needed
		free(itacacpad->snr_mat);
		itacacpad->snr_mat = NULL;
	}

	if(itacacpad->tmp_snr_mat) {
		free(itacacpad->tmp_snr_mat);
		itacacpad->tmp_snr_mat = NULL;
	}

	if(itacacpad->autocorrelation_matrix) {
		free(itacacpad->autocorrelation_matrix);
		itacacpad->autocorrelation_matrix = NULL;
	}

	if(itacacpad->autocorrelation_mask) {
		free(itacacpad->autocorrelation_mask);
		itacacpad->autocorrelation_mask = NULL;
	}

	if(itacacpad->autocorrelation_norm) {
		free(itacacpad->autocorrelation_norm);
		itacacpad->autocorrelation_norm = NULL;
	}

	if(itacacpad->chi2) {
		free(itacacpad->chi2);
		itacacpad->chi2 = NULL;
	}

	if(itacacpad->tmp_chi2) {
		free(itacacpad->tmp_chi2);
		itacacpad->tmp_chi2 = NULL;
	}

	//G_OBJECT_CLASS(gstlal_itacac_pad_parent_class)->finalize(object);
	G_OBJECT_CLASS(gstlal_itacac_pad_parent_class)->dispose(object);
}

/*
static void gstlal_itacac_finalize(GObject *object)
{
	guint i;
	GSTLALItacac *itacac = GSTLAL_ITACAC(object);

	//g_mutex_clear(&(GSTLAL_ITACAC_PAD_GET_CLASS(GSTLAL_ITACAC_PAD(element->sinkpads->data))->padlock)); FIXME Why doesnt this work?
	G_OBJECT_CLASS(gstlal_itacac_parent_class)->finalize(object);
}
*/


/*
 * Class init function.  See
 *
 * http://developer.gnome.org/doc/API/2.0/gobject/gobject-Type-Information.html#GClassInitFunc
 */


static void gstlal_itacac_pad_class_init(GSTLALItacacPadClass *klass)
{
	GObjectClass *gobject_class = G_OBJECT_CLASS(klass);

	gobject_class->set_property = GST_DEBUG_FUNCPTR(gstlal_itacac_pad_set_property);
	gobject_class->get_property = GST_DEBUG_FUNCPTR(gstlal_itacac_pad_get_property);
	// Right now no memory is allocated in the class instance structure for GSTLALItacacPads, so we dont need a custom finalize function
	// If anything is added to the class structure, we will need a custom finalize function that chains up to the AggregatorPad's finalize function
	gobject_class->dispose = GST_DEBUG_FUNCPTR(gstlal_itacac_pad_dispose);

	//
	// Properties
	//

        g_object_class_install_property(
		gobject_class,
		ARG_N,
		g_param_spec_uint(
			"n",
			"n",
			"number of samples over which to identify itacs",
			0, G_MAXUINT, 0,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | G_PARAM_CONSTRUCT
		)
	);

	g_object_class_install_property(
		gobject_class,
		ARG_BANK_FILENAME,
		g_param_spec_string(
			"bank-filename",
			"Bank file name",
			"Path to XML file used to generate the template bank.  Setting this property resets sigmasq to a vector of 0s.",
			NULL,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);

	g_object_class_install_property(
		gobject_class,
		ARG_SNR_THRESH,
		g_param_spec_double(
			"snr-thresh",
			"SNR Threshold",
			"SNR Threshold that determines a trigger.",
			0, G_MAXDOUBLE, DEFAULT_SNR_THRESH,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);

	g_object_class_install_property(
		gobject_class,
		ARG_SIGMASQ,
		g_param_spec_value_array(
			"sigmasq",
			"\\sigma^{2} factors",
			"Vector of \\sigma^{2} factors.  The effective distance of a trigger is \\sqrt{sigma^{2}} / SNR.",
			g_param_spec_double(
				"sigmasq",
				"\\sigma^{2}",
				"\\sigma^{2} factor",
				-G_MAXDOUBLE, G_MAXDOUBLE, 0.0,
				G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
			),
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | GST_PARAM_CONTROLLABLE
		)
	);


	g_object_class_install_property(
		gobject_class,
		ARG_AUTOCORRELATION_MATRIX,
		g_param_spec_value_array(
			"autocorrelation-matrix",
			"Autocorrelation Matrix",
			"Array of complex autocorrelation vectors.  Number of vectors (rows) in matrix sets number of channels.  All vectors must have the same length.",
			g_param_spec_value_array(
				"autocorrelation",
				"Autocorrelation",
				"Array of autocorrelation samples.",
				/* FIXME:  should be complex */
				g_param_spec_double(
					"sample",
					"Sample",
					"Autocorrelation sample",
					-G_MAXDOUBLE, G_MAXDOUBLE, 0.0,
					G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
				),
				G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
			),
		G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);

	g_object_class_install_property(
		gobject_class,
		ARG_AUTOCORRELATION_MASK,
		g_param_spec_value_array(
			"autocorrelation-mask",
			"Autocorrelation Mask Matrix",
			"Array of integer autocorrelation mask vectors.  Number of vectors (rows) in mask sets number of channels.  All vectors must have the same length. The mask values are either 0 or 1 and indicate whether to use the corresponding matrix entry in computing the autocorrelation chi-sq statistic.",
			g_param_spec_value_array(
				"autocorrelation-mask",
				"Autocorrelation-mask",
				"Array of autocorrelation mask values.",
				g_param_spec_int(
					"sample",
					"Sample",
					"Autocorrelation mask value",
					0, 1, 0,
					G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
				),
				G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
			),
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);

}

static void gstlal_itacac_class_init(GSTLALItacacClass *klass)
{
	GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
	GstAggregatorClass *aggregator_class = GST_AGGREGATOR_CLASS(klass); 

	gst_element_class_set_metadata(
		element_class,
		"Itacac",
		"Filter",
		"Find coincident inspiral triggers in snr streams from multiple detectors",
		"Cody Messick <cody.messick@ligo.org>"
	);

	//
	// Our custom functions
	//

	aggregator_class->aggregate = GST_DEBUG_FUNCPTR(aggregate);
	aggregator_class->sink_event = GST_DEBUG_FUNCPTR(sink_event);

	//
	// static pad templates
	//

	gst_element_class_add_static_pad_template_with_gtype(
		element_class,
		&sink_templ,
		GSTLAL_ITACAC_PAD_TYPE
	);

	gst_element_class_add_static_pad_template_with_gtype(
		element_class,
		&src_templ,
		GST_TYPE_AGGREGATOR_PAD
	);

	//
	// Properties
	//
}


/*
 * Instance init function.  See
 *
 * http://developer.gnome.org/doc/API/2.0/gobject/gobject-Type-Information.html#GInstanceInitFunc
 */

static void gstlal_itacac_pad_init(GSTLALItacacPad *itacacpad)
{
	itacacpad->adapter = g_object_new(GST_TYPE_AUDIOADAPTER, NULL);
	itacacpad->rate = 0;
	itacacpad->channels = 0;
	itacacpad->data = NULL;
	itacacpad->chi2 = NULL;
	itacacpad->tmp_chi2 = NULL;
	itacacpad->bank_filename = NULL;
	itacacpad->instrument = NULL;
	itacacpad->channel_name = NULL;
	itacacpad->difftime = 0;
	itacacpad->maxdata = NULL;
	itacacpad->tmp_maxdata = NULL;
	itacacpad->snr_thresh = 0;
	g_mutex_init(&itacacpad->bank_lock);

	itacacpad->autocorrelation_matrix = NULL;
	itacacpad->autocorrelation_mask = NULL;
	itacacpad->autocorrelation_norm = NULL;
	itacacpad->snr_mat = NULL;
	itacacpad->tmp_snr_mat = NULL;
	itacacpad->bankarray = NULL;
	itacacpad->last_gap = TRUE;

	itacacpad->adjust_window = 0;

	gst_pad_use_fixed_caps(GST_PAD(itacacpad));

}

static void gstlal_itacac_init(GSTLALItacac *itacac)
{
	itacac->rate = 0;
	itacac->channels = 0;
	itacac->n_ifos = 0; 

	itacac->difftime = 0;
	
	reset_time_and_offset(itacac);

	itacac->EOS = FALSE;

}
