# A gst-launch pipeline to encode h(t) into a OGG/Vorbis file

LALCACHE="/home/kipp/scratch_local/874000000-20063/cache/data.cache"
GPSSTART="874000000"
#GPSSTOP="874020000"
GPSSTOP="874000120"
INSTRUMENT="H1"
CHANNEL="LSC-STRAIN"
OUTPUT="hoft.ogm"

gst-launch \
	lal_framesrc \
		blocksize=65536 \
		location="${LALCACHE}" \
		instrument="${INSTRUMENT}" \
		channel-name="${CHANNEL}" \
		start-time-gps="${GPSSTART}" \
		stop-time-gps="${GPSSTOP}" \
	! audioconvert \
	! audio/x-raw-float, width=64 \
	! audiochebband \
		lower-frequency=45 \
		upper-frequency=2500 \
		poles=8 \
	! audioconvert \
	! audio/x-raw-float, width=32 \
	! audioamplify \
		amplification=2e+17 \
	! progressreport \
		update-freq=2 \
	! vorbisenc \
	! oggmux \
	! filesink \
		location="${OUTPUT}"
