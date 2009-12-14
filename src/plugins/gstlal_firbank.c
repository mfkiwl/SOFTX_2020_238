/*
 * Copyright (C) 2009 Kipp Cannon <kipp.cannon@ligo.org>
 * 
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the
 * "Software"), to deal in the Software without restriction, including
 * without limitation the rights to use, copy, modify, merge, publish,
 * distribute, sublicense, and/or sell copies of the Software, and to
 * permit persons to whom the Software is furnished to do so, subject to
 * the following conditions:
 *
 * The above copyright notice and this permission notice shall be included
 * in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
 * OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
 * IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
 * CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
 * TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * You should have received a copy of the GNU Library General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307,
 * USA.
 */


/*
 * ============================================================================
 *
 *                                  Preamble
 *
 * ============================================================================
 */


/*
 * stuff from the C library
 */


#include <complex.h>
#include <string.h>


/*
 *  stuff from gobject/gstreamer
 */


#include <glib.h>
#include <gst/gst.h>
#include <gst/base/gstadapter.h>
#include <gst/base/gstbasetransform.h>
#include "gstlal.h"
#include "gstlal_firbank.h"


/*
 * stuff from FFTW and GSL
 */


#include <fftw3.h>
#include <gsl/gsl_vector.h>
#include <gsl/gsl_matrix.h>
#include <gsl/gsl_blas.h>


/*
 * ============================================================================
 *
 *                                 Utilities
 *
 * ============================================================================
 */


/*
 * return the number of FIR vectors
 */


static unsigned fir_channels(const GSTLALFIRBank *element)
{
	return element->fir_matrix->size1;
}


/*
 * return the number of samples in the FIR vectors
 */


static unsigned fir_length(const GSTLALFIRBank *element)
{
	return element->fir_matrix->size2;
}


/*
 * return the number of time-domain samples in each FFT
 */


static unsigned fft_block_length(const GSTLALFIRBank *element)
{
	return fir_length(element) * element->block_length_factor;
}


/*
 * construct a buffer of zeros and push into adapter
 */


static int push_zeros(GSTLALFIRBank *element, unsigned samples)
{
	GstBuffer *zerobuf = gst_buffer_new_and_alloc(samples * sizeof(double));
	if(!zerobuf) {
		GST_DEBUG_OBJECT(element, "failure allocating zero-pad buffer");
		return -1;
	}
	memset(GST_BUFFER_DATA(zerobuf), 0, GST_BUFFER_SIZE(zerobuf));
	gst_adapter_push(element->adapter, zerobuf);
	element->zeros_in_adapter += samples;
	return 0;
}


/*
 * set the metadata on an output buffer
 */


static void set_metadata(GSTLALFIRBank *element, GstBuffer *buf, guint64 outsamples, gboolean gap)
{
	GST_BUFFER_SIZE(buf) = outsamples * fir_channels(element) * sizeof(double);
	GST_BUFFER_OFFSET(buf) = element->next_out_offset;
	element->next_out_offset += outsamples;
	GST_BUFFER_OFFSET_END(buf) = element->next_out_offset;
	GST_BUFFER_TIMESTAMP(buf) = element->t0 + gst_util_uint64_scale_int_round(GST_BUFFER_OFFSET(buf) - element->offset0, GST_SECOND, element->rate);
	GST_BUFFER_DURATION(buf) = element->t0 + gst_util_uint64_scale_int_round(GST_BUFFER_OFFSET_END(buf) - element->offset0, GST_SECOND, element->rate) - GST_BUFFER_TIMESTAMP(buf);
	if(element->need_discont) {
		GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
		element->need_discont = FALSE;
	}
	if(gap)
		GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_GAP);
	else
		GST_BUFFER_FLAG_UNSET(buf, GST_BUFFER_FLAG_GAP);
}


/*
 * the number of samples available in the adapter
 */


static guint64 get_available_samples(GSTLALFIRBank *element)
{
	return gst_adapter_available(element->adapter) / sizeof(double);
}


/*
 * create/free FFT workspace.  call with fir_matrix_lock held
 */


static int create_fft_workspace(GSTLALFIRBank *element)
{
	unsigned i;
	unsigned length_fd = fft_block_length(element) / 2 + 1;

	/*
	 * frequency-domain input
	 */

	g_mutex_lock(gstlal_fftw_lock);

	element->input_fd = (complex double *) fftw_malloc(length_fd * sizeof(*element->input_fd));
	element->in_plan = fftw_plan_dft_r2c_1d(fft_block_length(element), (double *) element->input_fd, element->input_fd, FFTW_MEASURE);

	/*
	 * frequency-domain workspace
	 */

	element->workspace_fd = (complex double *) fftw_malloc(length_fd * sizeof(*element->workspace_fd));
	element->out_plan = fftw_plan_dft_c2r_1d(fft_block_length(element), element->workspace_fd, (double *) element->workspace_fd, FFTW_MEASURE);

	/*
	 * loop over filters.  copy each time-domain filter to input_fd,
	 * zero-pad, transform to frequency domain, and save in
	 * fir_matrix_fd.  the frequency-domain filters are pre-scaled by
	 * 1/n and conjugated to save those operations inside the filtering
	 * loop.
	 */

	element->fir_matrix_fd = (complex double *) fftw_malloc(fir_channels(element) * length_fd * sizeof(*element->fir_matrix_fd));

	g_mutex_unlock(gstlal_fftw_lock);

	for(i = 0; i < fir_channels(element); i++) {
		unsigned j;
		memset(element->input_fd, 0, length_fd * sizeof(*element->input_fd));
		for(j = 0; j < fir_length(element); j++)
			((double *) element->input_fd)[j] = gsl_matrix_get(element->fir_matrix, i, j) / fft_block_length(element);
		fftw_execute(element->in_plan);
		for(j = 0; j < length_fd; j++)
			element->fir_matrix_fd[i * length_fd + j] = conj(element->input_fd[j]);
	}

	/*
	 * done
	 */

	return 0;
}


static void free_fft_workspace(GSTLALFIRBank *element)
{
	g_mutex_lock(gstlal_fftw_lock);

	fftw_free(element->fir_matrix_fd);
	element->fir_matrix_fd = NULL;
	fftw_free(element->input_fd);
	element->input_fd = NULL;
	fftw_destroy_plan(element->in_plan);
	element->in_plan = NULL;
	fftw_free(element->workspace_fd);
	element->workspace_fd = NULL;
	fftw_destroy_plan(element->out_plan);
	element->out_plan = NULL;

	g_mutex_unlock(gstlal_fftw_lock);
}


/*
 * transform input samples to output samples using a time-domain algorithm
 */


static GstFlowReturn tdfilter(GSTLALFIRBank *element, GstBuffer *outbuf)
{
	unsigned i;
	unsigned available_length;
	unsigned output_length;
	gsl_vector_view input;
	gsl_matrix_view output;

	/*
	 * how many samples can we construct from the contents of the
	 * adapter?  the +1 is because when there is 1 FIR-length of data
	 * in the adapter then we can produce 1 output sample, not 0.
	 */

	available_length = get_available_samples(element);
	if(available_length < fir_length(element))
		return GST_BASE_TRANSFORM_FLOW_DROPPED;
	output_length = available_length - fir_length(element) + 1;

	/*
	 * wrap the adapter's contents in a GSL vector view.  note that the
	 * wrapper vector's length is set to the fir_length length, not the
	 * length that has been peeked at, so that inner products work
	 * properly.
	 */

	input = gsl_vector_view_array((double *) gst_adapter_peek(element->adapter, available_length * sizeof(double)), fir_length(element));

	/*
	 * wrap output buffer in a GSL matrix view.
	 */

	output = gsl_matrix_view_array((double *) GST_BUFFER_DATA(outbuf), output_length, fir_channels(element));

	/*
	 * assemble the output sample time series as the columns of a
	 * matrix.
	 */

	for(i = 0; i < output_length; i++) {
		/*
		 * the current row (sample) in the output matrix
		 */

		gsl_vector_view output_sample = gsl_matrix_row(&(output.matrix), i);

		/*
		 * compute one vector of output samples --- the projection
		 * of the input onto each of the FIR filters
		 */

		gsl_blas_dgemv(CblasNoTrans, 1.0, element->fir_matrix, &(input.vector), 0.0, &(output_sample.vector));

		/*
		 * advance the input pointer
		 */

		input.vector.data++;
	}

	/*
	 * flush the data from the adapter
	 */

	gst_adapter_flush(element->adapter, output_length * sizeof(double));
	if(element->zeros_in_adapter > available_length - output_length)
		/*
		 * some trailing zeros have been flushed from the adapter
		 */

		element->zeros_in_adapter = available_length - output_length;

	/*
	 * set buffer metadata
	 */

	set_metadata(element, outbuf, output_length, FALSE);

	/*
	 * done
	 */

	return GST_FLOW_OK;
}


/*
 * transform input samples to output samples using a frequency-domain
 * algorithm
 */


static GstFlowReturn fdfilter(GSTLALFIRBank *element, GstBuffer *outbuf)
{
	unsigned i;
	unsigned fft_block_stride;
	unsigned fft_blocks;
	unsigned available_length;
	unsigned input_length;
	unsigned output_length;
	unsigned filter_length_fd;
	double *input;
	gsl_vector_view workspace;

	/*
	 * how many FFT blocks can we construct from the contents of the
	 * adapter?
	 */

	available_length = get_available_samples(element);
	if(available_length < fft_block_length(element))
		return GST_BASE_TRANSFORM_FLOW_DROPPED;
	fft_block_stride = fft_block_length(element) - fir_length(element) + 1;
	fft_blocks = (available_length - fft_block_length(element)) / fft_block_stride + 1;
	input_length = (fft_blocks - 1) * fft_block_stride + fft_block_length(element);
	output_length = input_length - fir_length(element) + 1;
	filter_length_fd = fft_block_length(element) / 2 + 1;

	/*
	 * retrieve input samples
	 */

	input = (double *) gst_adapter_peek(element->adapter, input_length * sizeof(double));

	/*
	 * wrap workspace (as real numbers) in a GSL vector view.  note
	 * that vector has length fft_block_stride to affect the requisite
	 * transient clipping
	 */

	workspace = gsl_vector_view_array((double *) element->workspace_fd, fft_block_stride);

	/*
	 * loop over FFT blocks
	 */

	for(i = 0; i < fft_blocks; i++) {
		gsl_matrix_view output;
		complex double *filter;
		unsigned j;

		/*
		 * wrap this block of output buffer in a GSL matrix view
		 */

		output = gsl_matrix_view_array(((double *) GST_BUFFER_DATA(outbuf)) + i * fft_block_stride * fir_channels(element), fft_block_stride, fir_channels(element));

		/*
		 * copy a block-length of data to input workspace and
		 * transform to frequency-domain
		 */

		memcpy(element->input_fd, input, fft_block_length(element) * sizeof(*input));
		fftw_execute(element->in_plan);

		/*
		 * loop over filters
		 */

		filter = element->fir_matrix_fd;
		for(j = 0; j < fir_channels(element); j++) {
			/*
			 * multiply input by filter, transform to
			 * time-domain, copy to output
			 */

			unsigned k;
			for(k = 0; k < filter_length_fd; k++)
				element->workspace_fd[k] = element->input_fd[k] * *(filter++);
			fftw_execute(element->out_plan);
			gsl_matrix_set_col(&output.matrix, j, &workspace.vector);
		}

		/*
		 * advance to next FFT block
		 */

		input += fft_block_stride;
	}

	/*
	 * flush the data from the adapter
	 */

	gst_adapter_flush(element->adapter, output_length * sizeof(double));
	if(element->zeros_in_adapter > available_length - output_length)
		/*
		 * some trailing zeros have been flushed from the adapter
		 */

		element->zeros_in_adapter = available_length - output_length;

	/*
	 * set buffer metadata
	 */

	set_metadata(element, outbuf, output_length, FALSE);

	/*
	 * done
	 */

	return GST_FLOW_OK;
}


/*
 * select a filtering algorithm
 */


static GstFlowReturn filter(GSTLALFIRBank *element, GstBuffer *outbuf)
{
#if 0
	return tdfilter(element, outbuf);
#else
	/*
	 * get frequency-domain filters if needed
	 */

	if(!element->fir_matrix_fd)
		create_fft_workspace(element);

	return fdfilter(element, outbuf);
#endif
}


/*
 * ============================================================================
 *
 *                           GStreamer Boiler Plate
 *
 * ============================================================================
 */


static GstStaticPadTemplate sink_factory = GST_STATIC_PAD_TEMPLATE(
	"sink",
	GST_PAD_SINK,
	GST_PAD_ALWAYS,
	GST_STATIC_CAPS(
		"audio/x-raw-float, " \
		"rate = (int) [1, MAX], " \
		"channels = (int) 1, " \
		"endianness = (int) BYTE_ORDER, " \
		"width = (int) 64"
	)
);


static GstStaticPadTemplate src_factory = GST_STATIC_PAD_TEMPLATE(
	"src",
	GST_PAD_SRC,
	GST_PAD_ALWAYS,
	GST_STATIC_CAPS(
		"audio/x-raw-float, " \
		"rate = (int) [1, MAX], " \
		"channels = (int) [1, MAX], " \
		"endianness = (int) BYTE_ORDER, " \
		"width = (int) 64"
	)
);


GST_BOILERPLATE(
	GSTLALFIRBank,
	gstlal_firbank,
	GstBaseTransform,
	GST_TYPE_BASE_TRANSFORM
);


enum property {
	ARG_BLOCK_LENGTH_FACTOR = 1,
	ARG_FIR_MATRIX,
	ARG_LATENCY
};


#define DEFAULT_BLOCK_LENGTH_FACTOR 4
#define DEFAULT_LATENCY 0


/*
 * ============================================================================
 *
 *                     GstBaseTransform Method Overrides
 *
 * ============================================================================
 */


/*
 * get_unit_size()
 */


static gboolean get_unit_size(GstBaseTransform *trans, GstCaps *caps, guint *size)
{
	GstStructure *str;
	gint channels;

	str = gst_caps_get_structure(caps, 0);
	if(!gst_structure_get_int(str, "channels", &channels)) {
		GST_DEBUG_OBJECT(trans, "unable to parse channels from %" GST_PTR_FORMAT, caps);
		return FALSE;
	}

	*size = sizeof(double) * channels;

	return TRUE;
}


/*
 * transform_caps()
 */


static GstCaps *transform_caps(GstBaseTransform *trans, GstPadDirection direction, GstCaps *caps)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	guint n;

	caps = gst_caps_copy(caps);

	switch(direction) {
	case GST_PAD_SRC:
		/*
		 * sink pad's format is the same as the source pad's except
		 * it must have only 1 channel
		 */

		for(n = 0; n < gst_caps_get_size(caps); n++)
			gst_structure_set(gst_caps_get_structure(caps, n), "channels", G_TYPE_INT, 1, NULL);
		break;

	case GST_PAD_SINK:
		/*
		 * source pad's format is the same as the sink pad's except
		 * it can have any number of channels or, if the size of
		 * the FIR matrix is known, the number of channels must
		 * equal the number of FIR filters.
		 */

		g_mutex_lock(element->fir_matrix_lock);
		for(n = 0; n < gst_caps_get_size(caps); n++) {
			if(element->fir_matrix)
				gst_structure_set(gst_caps_get_structure(caps, n), "channels", G_TYPE_INT, fir_channels(element), NULL);
			else
				gst_structure_set(gst_caps_get_structure(caps, n), "channels", GST_TYPE_INT_RANGE, 0, G_MAXINT, NULL);
		}
		g_mutex_unlock(element->fir_matrix_lock);
		break;

	case GST_PAD_UNKNOWN:
		GST_ELEMENT_ERROR(trans, CORE, NEGOTIATION, (NULL), ("invalid direction GST_PAD_UNKNOWN"));
		gst_caps_unref(caps);
		return GST_CAPS_NONE;
	}

	return caps;
}


/*
 * transform_size()
 */


static gboolean transform_size(GstBaseTransform *trans, GstPadDirection direction, GstCaps *caps, guint size, GstCaps *othercaps, guint *othersize)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	guint unit_size;
	guint other_unit_size;

	if(!get_unit_size(trans, caps, &unit_size))
		return FALSE;
	if(size % unit_size) {
		GST_DEBUG_OBJECT(element, "size not a multiple of %u", unit_size);
		return FALSE;
	}
	if(!get_unit_size(trans, othercaps, &other_unit_size))
		return FALSE;

	switch(direction) {
	case GST_PAD_SRC:
		/*
		 * just keep the sample count the same
		 */

		*othersize = (size / unit_size) * other_unit_size;
		break;

	case GST_PAD_SINK:
		/*
		 * upper bound of sample count on source pad is input
		 * sample count plus the number of samples in the adapter
		 * minus the impulse response length of the filters (-1
		 * because if there's 1 impulse response of data then we
		 * can generate 1 sample, not 0)
		 */

		*othersize = size / unit_size + get_available_samples(element);
		if(*othersize > fir_length(element) - 1)
			*othersize = (*othersize - (guint) fir_length(element) + 1) * other_unit_size;
		else
			*othersize = 0;
		break;

	case GST_PAD_UNKNOWN:
		GST_ELEMENT_ERROR(trans, CORE, NEGOTIATION, (NULL), ("invalid direction GST_PAD_UNKNOWN"));
		return FALSE;
	}

	return TRUE;
}


/*
 * set_caps()
 */


static gboolean set_caps(GstBaseTransform *trans, GstCaps *incaps, GstCaps *outcaps)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	GstStructure *s;
	gint rate;
	gint channels;

	s = gst_caps_get_structure(outcaps, 0);
	if(!gst_structure_get_int(s, "channels", &channels)) {
		GST_DEBUG_OBJECT(element, "unable to parse channels from %" GST_PTR_FORMAT, outcaps);
		return FALSE;
	}
	if(!gst_structure_get_int(s, "rate", &rate)) {
		GST_DEBUG_OBJECT(element, "unable to parse rate from %" GST_PTR_FORMAT, outcaps);
		return FALSE;
	}

	if(element->fir_matrix && (channels != (gint) fir_channels(element))) {
		GST_DEBUG_OBJECT(element, "channels != %d in %" GST_PTR_FORMAT, fir_channels(element), outcaps);
		return FALSE;
	}

	if(rate != element->rate) {
		/* FIXME:  emit "rate-changed" signal like gstreamer's
		 * audiofirfilter element does. */
	}

	element->rate = rate;

	return TRUE;
}


/*
 * start()
 */


static gboolean start(GstBaseTransform *trans)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	element->adapter = gst_adapter_new();
	element->zeros_in_adapter = 0;
	element->t0 = GST_CLOCK_TIME_NONE;
	element->offset0 = GST_BUFFER_OFFSET_NONE;
	element->next_in_offset = GST_BUFFER_OFFSET_NONE;
	element->next_out_offset = GST_BUFFER_OFFSET_NONE;
	element->need_discont = TRUE;
	return TRUE;
}


/*
 * stop()
 */


static gboolean stop(GstBaseTransform *trans)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	g_object_unref(element->adapter);
	element->adapter = NULL;
	return TRUE;
}


/*
 * transform()
 */


static GstFlowReturn transform(GstBaseTransform *trans, GstBuffer *inbuf, GstBuffer *outbuf)
{
	guint64 length;
	GSTLALFIRBank *element = GSTLAL_FIRBANK(trans);
	GstFlowReturn result;

	/*
	 * wait for FIR matrix
	 * FIXME:  add a way to get out of this loop
	 */

	g_mutex_lock(element->fir_matrix_lock);
	while(!element->fir_matrix)
		g_cond_wait(element->fir_matrix_available, element->fir_matrix_lock);

	/*
	 * check for discontinuity
	 *
	 * FIXME:  instead of reseting, flush/pad adapter as needed
	 */

	if(GST_BUFFER_OFFSET(inbuf) != element->next_in_offset || !GST_CLOCK_TIME_IS_VALID(element->t0)) {
		/*
		 * flush adapter
		 */

		gst_adapter_clear(element->adapter);
		element->zeros_in_adapter = 0;

		/*
		 * (re)sync timestamp and offset book-keeping
		 */

		element->t0 = GST_BUFFER_TIMESTAMP(inbuf);
		element->offset0 = GST_BUFFER_OFFSET(inbuf);
		element->next_out_offset = element->offset0 + fir_length(element) - 1 - element->latency;

		/*
		 * be sure to flag the next output buffer as a discontinuity
		 */

		element->need_discont = TRUE;
	}
	element->next_in_offset = GST_BUFFER_OFFSET_END(inbuf);

	/*
	 * gap logic
	 *
	 * FIXME:  gap handling with frequency-domain filtering is probably
	 * busted
	 */

	length = GST_BUFFER_OFFSET_END(inbuf) - GST_BUFFER_OFFSET(inbuf);
	if(!GST_BUFFER_FLAG_IS_SET(inbuf, GST_BUFFER_FLAG_GAP)) {
		/*
		 * input is not 0s.
		 */

		gst_buffer_ref(inbuf);	/* don't let the adapter free it */
		gst_adapter_push(element->adapter, inbuf);
		element->zeros_in_adapter = 0;
		result = filter(element, outbuf);
	} else if(element->zeros_in_adapter >= fir_length(element) - 1) {
		/*
		 * input is 0s and we are past the tail of the impulse
		 * response so output is all 0s.  output is a gap with the
		 * same number of samples as the input
		 */

		memset(GST_BUFFER_DATA(outbuf), 0, GST_BUFFER_SIZE(outbuf));
		set_metadata(element, outbuf, GST_BUFFER_OFFSET_END(inbuf) - GST_BUFFER_OFFSET(inbuf), TRUE);
		result = GST_FLOW_OK;
	} else if(element->zeros_in_adapter + length < fir_length(element)) {
		/*
		 * input is 0s, we are not yet past the tail of the impulse
		 * response and the input is not long enough to change
		 * that.  push length 0s into the adapter and run normal
		 * filtering
		 */

		push_zeros(element, length);
		result = filter(element, outbuf);
	} else {
		/*
		 * input is 0s, we are not yet past the tail of the impulse
		 * response, but the input is long enough to push us past
		 * the end.  this code path also handles the case of the
		 * first buffer being a gap, in which case
		 * available_samples is 0
		 */

		guint64 non_zero_samples = get_available_samples(element) - element->zeros_in_adapter;

		/*
		 * push (FIR-length - zeros-in-adapter - 1) 0s into adapter
		 * to allow previous non-zero data to be finished off.
		 * push_zeros() modifies zeros_in_adapter so update length
		 * first
		 */

		length -= fir_length(element) - element->zeros_in_adapter - 1;
		push_zeros(element, fir_length(element) - element->zeros_in_adapter - 1);

		/*
		 * run normal filter code to finish off adapter's contents,
		 * and manually push buffer downstream.
		 */

		if(non_zero_samples) {
			GstPad *srcpad = GST_BASE_TRANSFORM_SRC_PAD(GST_BASE_TRANSFORM(element));
			GstBuffer *buf;

			result = gst_pad_alloc_buffer(srcpad, element->next_out_offset, non_zero_samples * fir_channels(element) * sizeof(double), GST_PAD_CAPS(srcpad), &buf);
			if(result != GST_FLOW_OK)
				goto done;
			result = filter(element, buf);
			g_assert(result == GST_FLOW_OK);
			result = gst_pad_push(srcpad, buf);
			if(result != GST_FLOW_OK)
				goto done;
		}

		/*
		 * remainder of input produces 0s in output.  make outbuf a
		 * gap whose size matches the remainder of the input gap
		 */

		GST_BUFFER_SIZE(outbuf) = length * fir_channels(element) * sizeof(double);
		memset(GST_BUFFER_DATA(outbuf), 0, GST_BUFFER_SIZE(outbuf));
		set_metadata(element, outbuf, length, TRUE);
	}

	/*
	 * done
	 */

done:
	g_mutex_unlock(element->fir_matrix_lock);
	return result;
}


/*
 * ============================================================================
 *
 *                          GObject Method Overrides
 *
 * ============================================================================
 */


/*
 * set_property()
 */


static void set_property(GObject *object, enum property prop_id, const GValue *value, GParamSpec *pspec)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(object);

	GST_OBJECT_LOCK(element);

	switch (prop_id) {
	case ARG_BLOCK_LENGTH_FACTOR:
		g_mutex_lock(element->fir_matrix_lock);
		element->block_length_factor = g_value_get_int(value);

		/*
		 * invalidate frequency-domain filters
		 */

		free_fft_workspace(element);
		g_mutex_unlock(element->fir_matrix_lock);
		break;

	case ARG_FIR_MATRIX: {
		unsigned channels;
		g_mutex_lock(element->fir_matrix_lock);
		if(element->fir_matrix) {
			channels = fir_channels(element);
			gsl_matrix_free(element->fir_matrix);
		} else
			channels = 0;
		element->fir_matrix = gstlal_gsl_matrix_from_g_value_array(g_value_get_boxed(value));
		if(fir_channels(element) != channels)
			/*
			 * number of channels has changed, force a caps
			 * renegotiation
			 */
			gst_pad_set_caps(GST_BASE_TRANSFORM_SRC_PAD(GST_BASE_TRANSFORM(object)), NULL);

		/*
		 * invalidate frequency-domain filters
		 */

		free_fft_workspace(element);

		/*
		 * signal availability of new time-domain filters
		 */

		g_cond_broadcast(element->fir_matrix_available);
		g_mutex_unlock(element->fir_matrix_lock);
		break;
	}

	case ARG_LATENCY:
		element->latency = g_value_get_int64(value);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(element);
}


/*
 * get_property()
 */


static void get_property(GObject *object, enum property prop_id, GValue *value, GParamSpec *pspec)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(object);

	GST_OBJECT_LOCK(element);

	switch (prop_id) {
	case ARG_BLOCK_LENGTH_FACTOR:
		g_value_set_int(value, element->block_length_factor);
		break;

	case ARG_FIR_MATRIX:
		g_mutex_lock(element->fir_matrix_lock);
		if(element->fir_matrix)
			g_value_take_boxed(value, gstlal_g_value_array_from_gsl_matrix(element->fir_matrix));
		/* FIXME:  else? */
		g_mutex_unlock(element->fir_matrix_lock);
		break;

	case ARG_LATENCY:
		g_value_set_int64(value, element->latency);
		break;

	default:
		G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
		break;
	}

	GST_OBJECT_UNLOCK(element);
}


/*
 * finalize()
 */


static void finalize(GObject *object)
{
	GSTLALFIRBank *element = GSTLAL_FIRBANK(object);

	g_mutex_free(element->fir_matrix_lock);
	element->fir_matrix_lock = NULL;
	g_cond_free(element->fir_matrix_available);
	element->fir_matrix_available = NULL;
	if(element->fir_matrix) {
		gsl_matrix_free(element->fir_matrix);
		element->fir_matrix = NULL;
	}

	free_fft_workspace(element);

	G_OBJECT_CLASS(parent_class)->finalize(object);
}


/*
 * base_init()
 */


static void gstlal_firbank_base_init(gpointer gclass)
{
	GstElementClass *element_class = GST_ELEMENT_CLASS(gclass);
	GstBaseTransformClass *transform_class = GST_BASE_TRANSFORM_CLASS(gclass);

	gst_element_class_set_details_simple(element_class, "FIR Filter Bank", "Filter/Audio", "Projects a single audio channel onto a bank of FIR filters to produce a multi-channel output", "Kipp Cannon <kipp.cannon@ligo.org>");

	gst_element_class_add_pad_template(element_class, gst_static_pad_template_get(&src_factory));
	gst_element_class_add_pad_template(element_class, gst_static_pad_template_get(&sink_factory));

	transform_class->get_unit_size = GST_DEBUG_FUNCPTR(get_unit_size);
	transform_class->set_caps = GST_DEBUG_FUNCPTR(set_caps);
	transform_class->transform = GST_DEBUG_FUNCPTR(transform);
	transform_class->transform_caps = GST_DEBUG_FUNCPTR(transform_caps);
	transform_class->transform_size = GST_DEBUG_FUNCPTR(transform_size);
	transform_class->start = GST_DEBUG_FUNCPTR(start);
	transform_class->stop = GST_DEBUG_FUNCPTR(stop);
}


/*
 * class_init()
 */


static void gstlal_firbank_class_init(GSTLALFIRBankClass *klass)
{
	GObjectClass *gobject_class = G_OBJECT_CLASS(klass);

	gobject_class->set_property = GST_DEBUG_FUNCPTR(set_property);
	gobject_class->get_property = GST_DEBUG_FUNCPTR(get_property);
	gobject_class->finalize = GST_DEBUG_FUNCPTR(finalize);

	g_object_class_install_property(
		gobject_class,
		ARG_BLOCK_LENGTH_FACTOR,
		g_param_spec_int(
			"block-length-factor",
			"Convolution block size in multiples of the FIR length",
			"When using FFT convolutions, use this many times the number of samples in each FIR vector for the convolution block size.",
			2, G_MAXINT, DEFAULT_BLOCK_LENGTH_FACTOR,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);
	g_object_class_install_property(
		gobject_class,
		ARG_FIR_MATRIX,
		g_param_spec_value_array(
			"fir-matrix",
			"FIR Matrix",
			"Array of impulse response vectors.  Number of vectors (rows) in matrix sets number of output channels.  All filters must have the same length.",
			g_param_spec_value_array(
				"response",
				"Impulse Response",
				"Array of amplitudes.",
				g_param_spec_double(
					"amplitude",
					"Amplitude",
					"Impulse response sample",
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
		ARG_LATENCY,
		g_param_spec_int64(
			"latency",
			"Latency",
			"Filter latency in samples.",
			G_MININT64, G_MAXINT64, DEFAULT_LATENCY,
			G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS
		)
	);
}


/*
 * init()
 */


static void gstlal_firbank_init(GSTLALFIRBank *filter, GSTLALFIRBankClass *kclass)
{
	filter->block_length_factor = DEFAULT_BLOCK_LENGTH_FACTOR;
	filter->latency = DEFAULT_LATENCY;
	filter->adapter = NULL;
	filter->fir_matrix_lock = g_mutex_new();
	filter->fir_matrix_available = g_cond_new();
	filter->fir_matrix = NULL;
	filter->fir_matrix_fd = NULL;
	filter->input_fd = NULL;
	filter->workspace_fd = NULL;
	filter->in_plan = NULL;
	filter->out_plan = NULL;
	gst_base_transform_set_gap_aware(GST_BASE_TRANSFORM(filter), TRUE);
}
