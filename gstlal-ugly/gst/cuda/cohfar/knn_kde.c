/*
 * Copyright (C) 2017 Teresa, Xingjiang Zhu, Qi Chu <qi.chu@ligo.org>
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


#include <math.h>
#include <cohfar/knn_kde.h>

static int get_num_nonzero(gsl_matrix_long *histogram)
{
	
	int i = 0,j = 0,num_nonzero = 0;
	int x_nbin = histogram->size1, y_nbin = histogram->size1;
	for(i=0;i<x_nbin*y_nbin;i++)
	{
		if (gsl_matrix_long_get(histogram, i, j) > 0)
		{
			num_nonzero++;
		}
	}
	return num_nonzero;
}

static void set_nonzero_idx(gsl_matrix_long *histogram, gsl_matrix_long * nonzero_idx)
{
//	This loop should generate an array that looks something like this
//	0 1
//	0 3
//	...
//	1 0
//	1 2
//	...
//	...



	int i = 0,j = 0;
	int x_nbin = histogram->size1, y_nbin = histogram->size1;
	int inonzero = 0;
	for(i=0;i<x_nbin;i++) {
		for(j=0;j<y_nbin;j++) {
			if (gsl_matrix_long_get(histogram, i, j) > 0)
			{
				gsl_matrix_long_set(nonzero_idx, inonzero, 0, i);
				gsl_matrix_long_set(nonzero_idx, inonzero, 1, j);
				inonzero++;
			}
		}	
	}
}

static float get_kth_value(float * all_dist, int len, int knn_k)// Puts the distances from reference point to all data points into ascending order 
{
	int i=0;
	int j=0;
	for (i=0; i<len; i++)
	{
		for (j=0; j<len; j++)
		{
			if (all_dist[i] < all_dist[j])
			{

				float t = all_dist[i];
				all_dist[i] = all_dist[j];
				all_dist[j] = t;
			}
		}
	}
	float kthVal = all_dist[knn_k-1];
	return kthVal;
}


static void set_kth_dist(gsl_matrix_long * nonzero_idx, int knn_k, gsl_vector *kth_dist)// Calculates the distance from each grid point to each data point, calling ascend() to order them 
{

	int i = 0,j = 0;
	int num_nonzero = nonzero_idx->size1;
	float * all_dist = (float*)malloc(sizeof(float)*num_nonzero);
	float kth_value = 0;
	for (i=0;i<num_nonzero;i++)
	{
		for (j=0;j<num_nonzero;j++)
		{
			all_dist[j] = sqrt(pow(gsl_matrix_long_get(nonzero_idx, i, 0) - gsl_matrix_long_get(nonzero_idx, j, 0) , 2) + pow(gsl_matrix_long_get(nonzero_idx, i, 1) - gsl_matrix_long_get(nonzero_idx, j, 1), 2));
		}
		kth_value = get_kth_value(all_dist, num_nonzero, knn_k);
		gsl_vector_set(kth_dist, i, kth_value);
	}
	free(all_dist);
}

static void set_pdf(double band_const, gsl_vector *tin_x, gsl_vector *tin_y, gsl_matrix_long * histogram, gsl_matrix_long * nonzero_idx, gsl_vector * kth_dist, gsl_matrix *pdf)
{
		
	int i = 0,j = 0, k = 0;
	int x_nbin = histogram->size1, y_nbin = histogram->size1;
	int knn_x_idx, knn_y_idx;
	int num_nonzero = nonzero_idx->size1;
	long nevent = gsl_matrix_long_sum(histogram);
	double dist, gau, sum_gau = 0.0;
	double cur_x_coor, cur_y_coor, knn_x_coor, knn_y_coor;
	double norm_factor = 0, hband;

	for(i=0;i<x_nbin;i++)
	{
		for(j=0;j<y_nbin;j++)
		{
			sum_gau = 0;
			for(k=0;k<num_nonzero;k++)
			{
				cur_x_coor = gsl_vector_get(tin_x, i); 
				cur_y_coor = gsl_vector_get(tin_y, j); 
				knn_x_idx = (int) gsl_matrix_long_get(nonzero_idx, k, 0);
				knn_y_idx = (int) gsl_matrix_long_get(nonzero_idx, k, 1);
				knn_x_coor = gsl_vector_get(tin_x, knn_x_idx); 
				knn_y_coor = gsl_vector_get(tin_y, knn_y_idx); 
				hband = band_const* gsl_vector_get(kth_dist, k);
				dist = -(pow(cur_x_coor - knn_x_coor, 2) + pow(cur_y_coor - knn_y_coor, 2))/(2 * pow(hband, 2));
				gau = (double)(1/nevent) * exp(dist)*((double)gsl_matrix_long_get(histogram, knn_x_idx, knn_y_idx))/(2 * M_PI * pow(hband, 2));
				sum_gau = sum_gau + gau;
			}
			gsl_matrix_set(pdf, i, j, sum_gau);
			printf("%f ",sum_gau);
		}
		printf("\n");
	}
	/* normalization that sum(pdf) == 1 */
	norm_factor = 1/ gsl_matrix_sum(pdf);
	gsl_matrix_scale(pdf, norm_factor);
}

void
knn_kde(gsl_vector *tin_x, gsl_vector *tin_y, gsl_matrix_long *histogram, gsl_matrix *pdf)
{
	int knn_k = 11;
	double band_const = 0.4;
	int num_nonzero = get_num_nonzero(histogram);

	gsl_matrix_long * nonzero_idx = gsl_matrix_long_calloc(num_nonzero, 2);
	set_nonzero_idx(histogram, nonzero_idx);

	gsl_vector * kth_dist = gsl_vector_alloc(num_nonzero);
	set_kth_dist(nonzero_idx, knn_k, kth_dist);
	set_pdf(band_const, tin_x, tin_y, histogram, nonzero_idx, kth_dist, pdf);

}
