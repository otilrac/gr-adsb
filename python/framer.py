#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 
# Copyright 2016 Matt Hostetter.
# 
# This is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this software; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
# 

import numpy as np
from gnuradio import gr
import pmt

class framer(gr.sync_block):
    """
    docstring for block framer
    """
    def __init__(self, fs, burst_thresh):
        gr.sync_block.__init__(self,
            name="ADS-B Framer",
            in_sig=[np.float32],
            out_sig=[np.float32])

        # Calculate the samples/symbol
        # ADS-B is modulated at 1 Msym/s with Pulse Position Modulation, so the effective
        # required fs is 2 Msps
        self.sps = fs/(1e6) 
        if (self.sps - np.floor(self.sps)) > 0:
            print "Warning: ADS-B Framer is designed to operate on an integer number of samples per symbol"
        self.sps = int(self.sps) # Set the samples/symbol to an integer

        self.burst_thresh = burst_thresh

        # Initialize the preamble "pulses" template
        # This is 2*fsym or 2 Msps, i.e. there are 2 pulses per symbol
        self.preamble_pulses = [1,0,1,0,0,0,0,1,0,1,0,0,0,0,0,0]
        
        # Last sample from previous work() call.  Needed for finding pulses at 
        # the beginning of the current work() call.        
        self.prev_in0 = 0 

        # End of the last burst (56 bit message).  Don't look for preambles during a valid packet
        self.last_eob_idx = -1

        self.straddled_packet = 0

        # Propagate tags
        self.set_tag_propagation_policy(gr.TPP_ONE_TO_ONE)

        print "\nInitialized ADS-B Framer:"
        print "  Sampling Rate:       %1.2f Msps" % (fs/1e6)
        print "  Samples Per Symbol:  %d" % (self.sps)
        print "  Burst Threshold:     %1.4f" % (self.burst_thresh)


    def work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]

        # Create a binary array that represents when the input goes above
        # the threshold value
        # NOTE: Add the last sample from the previous work() call to the 
        # beginning of this block of samples
        in0_pulses = np.zeros(len(in0)+1, dtype=int)
        in0_pulses[np.insert(in0, 0, self.prev_in0) >= self.burst_thresh] = 1

        # Set prev_in0 for the next work() call
        self.prev_in0 = in0[-1]

        # Subtract the previous pulse from the current pulse to get transitions
        # +1 = rising edge, -1 = falling edge
        in0_transitions = in0_pulses[1:] - in0_pulses[:-1]

        in0_rise_edge_idxs = np.nonzero(in0_transitions == 1)[0]
        in0_fall_edge_idxs = np.nonzero(in0_transitions == -1)[0]

        # Make sure there is one and only one falling edge for each rising edge
        if len(in0_rise_edge_idxs) > 0 and len(in0_fall_edge_idxs) > 0:
            # Make sure the first sample for the rising and falling edge indices corresponds
            # to the same pulse
            if in0_fall_edge_idxs[0] - in0_rise_edge_idxs[0] < 0:
                # The first falling edge comes before the first rising edge, so remove it
                in0_fall_edge_idxs = np.delete(in0_fall_edge_idxs, 0)

            if len(in0_rise_edge_idxs) - len(in0_fall_edge_idxs) > 0:
                # If there are more rising edges than falling edges, then
                # remove the extras
                # NOTE: There technically can only possibly be 1 extra rising edge, if 
                # there are more, something went terribly wrong
                if len(in0_rise_edge_idxs) - len(in0_fall_edge_idxs) == 1:
                    in0_rise_edge_idxs = np.delete(in0_rise_edge_idxs, len(in0_rise_edge_idxs)-1)
                else:
                    print "Oh no, this shouldn't be happening..."

        # Find the index of the center of each pulses
        if len(in0_rise_edge_idxs) > 0:
            pulse_idxs = np.mean((in0_fall_edge_idxs,in0_rise_edge_idxs),axis=0).astype(int)
        else:
            pulse_idxs = []

        # For each pulse found, check if that pulse is the beginning of the ADS-B
        # preamble.
        for pulse_idx in pulse_idxs:
            # Only process this pulse if it's not a pulse from the previous packet.
            # There will be many "pulses" in a valid packet and we don't want to waster
            # cycles looking for preambles where they won't be
            if pulse_idx > self.last_eob_idx:
                # Reset EOB index so we don't trigger on it later
                self.last_eob_idx = -1

                # Tag the detected pulses for debug
                if 0:
                    self.add_item_tag(  0, 
                        self.nitems_written(0)+pulse_idx,
                        pmt.to_pmt("pulse"),
                        pmt.to_pmt("1"),    
                        pmt.to_pmt("framer")
                    )

                # If there are enough samples for the preamble to be completely contained 
                # in this set of samples, then check for a preamble correlation
                if pulse_idx + len(self.preamble_pulses)*self.sps < len(in0):
                    # Starting at the center of the discovered pulse, find the amplitudes of each 
                    # half symbol and then compare it to what the preamble symbols
                    amps = in0[pulse_idx:(pulse_idx+len(self.preamble_pulses)*self.sps/2):(self.sps/2)]

                    # Set a pulse to 1 if it's greater than 1/2 the amplitude of the detected pulse
                    pulses = np.zeros(len(self.preamble_pulses), dtype=int)
                    pulses[amps > in0[pulse_idx]/2] = 1

                    # Count how many "pulses" or 1/2 symbols match the preamble "pulses"
                    corr_matches = np.sum(pulses == self.preamble_pulses)

                    # Only assert preamble found if all the 1/2 symbols match
                    if corr_matches == len(self.preamble_pulses):
                        # Found a preamble correlation
                        # NOTE: in0[] is already a power vector I^2 + Q^2, so to compute power
                        # SNR we take 10*log10().
                        # NOTE: The median of a Rayleigh distributed random variable is 1.6 dB
                        # less than the average.  So add 1.6 dB to get a more accurate power
                        # SNR.
                        num_noise_samples = 100
                        if pulse_idx < num_noise_samples:
                            snr = 10.0*np.log10(in0[pulse_idx]/np.median(in0[0:pulse_idx])) + 1.6
                        else:
                            snr = 10.0*np.log10(in0[pulse_idx]/np.median(in0[pulse_idx-100:pulse_idx])) + 1.6
                                                
                        # Calculate when this burst will end so we don't have to trigger
                        # off of all the "pulses" in this packet
                        # NOTE: Assume the shorter 56 bit packet because we don't yet know
                        # the packet length
                        self.last_eob_idx = pulse_idx + (8+56-1)*self.sps

                        # Tag the start of the burst (preamble)
                        self.add_item_tag(  0,
                                            self.nitems_written(0)+pulse_idx,
                                            pmt.to_pmt("burst"),
                                            pmt.to_pmt(("SOB", snr)),
                                            pmt.to_pmt("framer")
                                        )

        # Check if the end of this burst will be in the next work() call
        if self.last_eob_idx >= len(in0):
            # Wrap the index so it's ready for the next work() call
            self.last_eob_idx -= len(in0)

        out0[:] = in0
        return len(output_items[0])
