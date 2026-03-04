#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# Headless GNU Radio flowgraph for Meshtastic LoRa RX with SDRplay RSPdx R2.
# Same signal processing chain as lora_rx_sdrplay.py but without Qt/GUI deps.
# All parameters configurable via environment variables.

import os
import signal
import sys

from gnuradio import gr
from gnuradio import sdrplay3
from gnuradio import zeromq
import gnuradio.lora_sdr as lora_sdr
import numpy as np


class lora_rx_sdrplay_headless(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "LoRa RX - SDRplay RSPdx R2 (headless)", catch_exceptions=True)

        ##################################################
        # Variables (from env vars with original defaults)
        ##################################################
        self.center_freq = float(os.environ.get("CENTER_FREQ", "913.125e6"))
        self.samp_rate = int(float(os.environ.get("SAMP_RATE", "1000000")))
        self.bw = int(float(os.environ.get("BW", "250000")))
        self.sf = int(os.environ.get("SF", "9"))
        self.cr = int(os.environ.get("CR", "1"))
        self.pay_len = int(os.environ.get("PAY_LEN", "255"))
        self.has_crc = os.environ.get("HAS_CRC", "True").lower() in ("true", "1", "yes")
        self.impl_head = os.environ.get("IMPL_HEAD", "False").lower() in ("true", "1", "yes")
        self.soft_decoding = os.environ.get("SOFT_DECODING", "True").lower() in ("true", "1", "yes")
        self.if_gain = int(os.environ.get("IF_GAIN", "40"))
        self.lna_state = int(os.environ.get("LNA_STATE", "10"))
        self.antenna = os.environ.get("ANTENNA", "Antenna A")

        center_freq = self.center_freq
        samp_rate = self.samp_rate
        bw = self.bw
        sf = self.sf
        cr = self.cr
        pay_len = self.pay_len
        has_crc = self.has_crc
        impl_head = self.impl_head
        soft_decoding = self.soft_decoding

        ##################################################
        # Blocks
        ##################################################

        # ZMQ pub sinks (same ports as GUI version)
        self.zeromq_pub_sink_1 = zeromq.pub_sink(
            gr.sizeof_gr_complex, 1, 'tcp://0.0.0.0:20003', 100, False, (-1), '', True, True)
        self.zeromq_pub_sink_0 = zeromq.pub_sink(
            gr.sizeof_char, 1, 'tcp://0.0.0.0:20002', 100, False, (-1), '', True, True)

        # SDRplay RSPdx R2 source
        self.sdrplay3_rspdxr2_0 = sdrplay3.rspdxr2(
            '',
            stream_args=sdrplay3.stream_args(
                output_type='fc32',
                channels_size=1
            ),
        )
        self.sdrplay3_rspdxr2_0.set_sample_rate(samp_rate, False)
        self.sdrplay3_rspdxr2_0.set_center_freq(center_freq, False)
        self.sdrplay3_rspdxr2_0.set_bandwidth(0)
        self.sdrplay3_rspdxr2_0.set_antenna(self.antenna)
        self.sdrplay3_rspdxr2_0.set_gain_mode(False)
        self.sdrplay3_rspdxr2_0.set_gain(-(self.if_gain), 'IF', False)
        self.sdrplay3_rspdxr2_0.set_gain(self.lna_state, 'LNAstate', False)
        self.sdrplay3_rspdxr2_0.set_freq_corr(0)
        self.sdrplay3_rspdxr2_0.set_dc_offset_mode(False)
        self.sdrplay3_rspdxr2_0.set_iq_balance_mode(False)
        self.sdrplay3_rspdxr2_0.set_agc_setpoint((-30))
        self.sdrplay3_rspdxr2_0.set_hdr_mode(False)
        self.sdrplay3_rspdxr2_0.set_rf_notch_filter(False)
        self.sdrplay3_rspdxr2_0.set_dab_notch_filter(False)
        self.sdrplay3_rspdxr2_0.set_biasT(False)
        self.sdrplay3_rspdxr2_0.set_debug_mode(False)
        self.sdrplay3_rspdxr2_0.set_sample_sequence_gaps_check(False)
        self.sdrplay3_rspdxr2_0.set_show_gain_changes(False)
        self.sdrplay3_rspdxr2_0.set_min_output_buffer((int(np.ceil(samp_rate / bw * (2**sf + 2)))))

        # LoRa demodulation chain
        self.lora_sdr_frame_sync_0 = lora_sdr.frame_sync(
            int(center_freq), bw, sf, impl_head, [0, 0], (int(samp_rate / bw)), 17)
        self.lora_sdr_fft_demod_0 = lora_sdr.fft_demod(soft_decoding, True)
        self.lora_sdr_gray_mapping_0 = lora_sdr.gray_mapping(soft_decoding)
        self.lora_sdr_deinterleaver_0 = lora_sdr.deinterleaver(soft_decoding)
        self.lora_sdr_hamming_dec_0 = lora_sdr.hamming_dec(soft_decoding)
        self.lora_sdr_header_decoder_0 = lora_sdr.header_decoder(
            impl_head, cr, pay_len, has_crc, False, False)
        self.lora_sdr_dewhitening_0 = lora_sdr.dewhitening()
        self.lora_sdr_crc_verif_0 = lora_sdr.crc_verif(2, False)

        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.lora_sdr_header_decoder_0, 'frame_info'),
                         (self.lora_sdr_frame_sync_0, 'frame_info'))
        self.connect((self.lora_sdr_crc_verif_0, 0), (self.zeromq_pub_sink_0, 0))
        self.connect((self.lora_sdr_deinterleaver_0, 0), (self.lora_sdr_hamming_dec_0, 0))
        self.connect((self.lora_sdr_dewhitening_0, 0), (self.lora_sdr_crc_verif_0, 0))
        self.connect((self.lora_sdr_fft_demod_0, 0), (self.lora_sdr_gray_mapping_0, 0))
        self.connect((self.lora_sdr_frame_sync_0, 0), (self.lora_sdr_fft_demod_0, 0))
        self.connect((self.lora_sdr_gray_mapping_0, 0), (self.lora_sdr_deinterleaver_0, 0))
        self.connect((self.lora_sdr_hamming_dec_0, 0), (self.lora_sdr_header_decoder_0, 0))
        self.connect((self.lora_sdr_header_decoder_0, 0), (self.lora_sdr_dewhitening_0, 0))
        self.connect((self.sdrplay3_rspdxr2_0, 0), (self.lora_sdr_frame_sync_0, 0))
        self.connect((self.sdrplay3_rspdxr2_0, 0), (self.zeromq_pub_sink_1, 0))


def main():
    tb = lora_rx_sdrplay_headless()

    def sig_handler(sig=None, frame=None):
        print(f"\n[headless] Caught signal {sig}, shutting down...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    print("[headless] Starting LoRa RX flowgraph")
    print(f"  CENTER_FREQ = {tb.center_freq}")
    print(f"  SAMP_RATE   = {tb.samp_rate}")
    print(f"  BW          = {tb.bw}")
    print(f"  SF          = {tb.sf}")
    print(f"  CR          = {tb.cr}")
    print(f"  IF_GAIN     = {tb.if_gain}")
    print(f"  LNA_STATE   = {tb.lna_state}")
    print(f"  ANTENNA     = {tb.antenna}")

    tb.start()
    tb.wait()


if __name__ == '__main__':
    main()
