# A gst-launch pipeline to encode h(t) from NDS2 into a OGG/Vorbis file
# The 'adder' element is included as a hack to throw away GPS time stamps; otherwise
# the autoaudiosink won't play

HOST="marble.ligo-wa.caltech.edu"
REQUESTED_CHANNEL_NAME="H1:DMT-STRAIN"

gst-launch \
	ndssrc \
        host="${HOST}" \
        requested-channel-name="${REQUESTED_CHANNEL_NAME}" \
	! audiochebband \
		lower-frequency=45 \
		upper-frequency=2500 \
		poles=8 \
	! audioamplify \
		clipping-method=3 \
		amplification=2e+17 \
	! progressreport \
		update-freq=2 \
	! adder \
	! audiorate \
	! audioconvert \
	! autoaudiosink
