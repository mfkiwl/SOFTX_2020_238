/*
 * Copyright (C) 2016 Aaron Viets <aaron.viets@ligo.org>
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
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
 */


#ifndef __GSTLAL_RESAMPLE_H__
#define __GSTLAL_RESAMPLE_H__

#include <complex.h>

#include <glib.h>
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>


G_BEGIN_DECLS
#define GSTLAL_RESAMPLE_TYPE \
	(gstlal_resample_get_type())
#define GSTLAL_RESAMPLE(obj) \
	(G_TYPE_CHECK_INSTANCE_CAST((obj), GSTLAL_RESAMPLE_TYPE, GSTLALResample))
#define GSTLAL_RESAMPLE_CLASS(klass) \
	(G_TYPE_CHECK_CLASS_CAST((klass), GSTLAL_RESAMPLE_TYPE, GSTLALResampleClass))
#define GST_IS_GSTLAL_RESAMPLE(obj) \
	(G_TYPE_CHECK_INSTANCE_TYPE((obj), GSTLAL_RESAMPLE_TYPE))
#define GST_IS_GSTLAL_RESAMPLE_CLASS(klass) \
	(G_TYPE_CHECK_CLASS_TYPE((klass), GSTLAL_RESAMPLE_TYPE))


typedef struct _GSTLALResample GSTLALResample;
typedef struct _GSTLALResampleClass GSTLALResampleClass;


/*
 * GSTLALResample:
 */


struct _GSTLALResample {
	GstBaseTransform element;

	/* stream info */

	guint rate_in;
	guint rate_out;
	guint unit_size;
	enum gstlal_resample_data_type {
		GSTLAL_RESAMPLE_F32 = 0,
		GSTLAL_RESAMPLE_F64,
		GSTLAL_RESAMPLE_Z64,
		GSTLAL_RESAMPLE_Z128
	} data_type;
	gboolean need_buffer_resize;
	guint leading_samples;

	/* timestamp book-keeping */

	GstClockTime t0;
	guint64 offset0;
	guint64 next_in_offset;
	guint64 next_out_offset;
	gboolean need_discont;
	gboolean need_gap;

	/* properties */
	guint polynomial_order;

	/* filter */
	double complex dxdt0;
	double complex *end_sample;
	double complex *before_end_sample;
};


/*
 * GSTLALResampleClass:
 * @parent_class:  the parent class
 */


struct _GSTLALResampleClass {
	GstBaseTransformClass parent_class;
};


GType gstlal_resample_get_type(void);


G_END_DECLS


#endif  /* __GSTLAL_RESAMPLE_H__ */
