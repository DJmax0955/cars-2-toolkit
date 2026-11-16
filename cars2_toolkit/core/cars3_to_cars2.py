#!/usr/bin/env python3
"""
cars3_to_cars2.py - backport Cars 3: Driven to Win animation clips to
Cars 2: The Video Game.

    offsetting oct decode  shrd_bm_inair.oct  shrd_bm_inair.json
    python cars3_to_cars2.py shrd_bm_inair.json -o shared_bm_inair.json
    offsetting oct encode  shared_bm_inair.json shared_bm_inair.oct

.oct input also works directly (python cars3_to_cars2.py shrd_bm_inair.oct -o ...).

Output templates (--mode):
  game       car_body + car_facial (default for everything but frontend)
  frontend   car_frontend_frontend (auto-picked for *_fe_* / select_* clips)
  a,b,...    any Cars 2 templates by name, e.g. car_body,car_rattles or
             car_eyes. Needed when the clip goes into a Rattles, Eyes, Eyelids,
             BodyBlend, LeanClipSet, ChoBlender or Mouth clip set; see
             clip_slot_map.py, which works this out per slot.

What differs between the two games, and what this script does about it
---------------------------------------------------------------------
Both games store a clip as one ClipDataBlock: a header, one or more "bind"
blocks, the animation commands, and a constant pool. Curve data (the
commands) is byte-for-byte the same format, so it is copied unchanged.
Everything else is translated:

  * Channel addressing. Cars 3 has ONE template ("cars3.ct", 205 ports,
    ordered by case-insensitive sort of "Node_Ctrl/Transform" or
    "Node_Ctrl/attr" keys).
    Cars 2 splits channels over several (car_body, car_facial, ...), each
    with its own index space. Every Cars 3 port that has a Cars 2 equivalent
    is remapped through PORT_MAP below, and the clip is split into one Cars 2
    bind per template.
  * Record layout. Cars 3 packs "port | nsub<<13" into a header array ahead
    of the sub-ops; Cars 2 interleaves a (nsub, target) pair before each
    record's sub-ops. Static records are (op, slot) pairs in Cars 3 and
    inline sub-ops in Cars 2.
  * Field ops. Cars 3 numbers the custom-float op 10 where Cars 2 uses 9.
  * Buffer layout. Each Cars 2 bind lists only the commands it needs, so
    buffer slot numbers are renumbered and the slot count rewritten.
  * Header. The Cars 2 JSON has no External/MinTime/MaxTime/AssetPool
    envelope; it has one ClipData node and one LayerBind per template.

Rotation order needs no conversion: the Cars 2 executable dispatches on all
six Maya rotation orders (order lives in bits 6-8 of the sub-op word), so a
Cars 3 clip that uses order 0 works as-is.

Channels with no Cars 2 counterpart (per-wheel spin, the Main_Ctrl custom
attributes, Cars 3-only FX markers, ...) are dropped and reported.

PORT_MAP evidence tiers:
  verified-graph      resolved through the Cars 3 rig's processor wiring
  verified-order      an ordered run that matches Cars 2 index-for-index
  verified-empirical  confirmed against clip data (Main: the 6*pi spin)
  verified-curve      the same animation curve drives this port in a Cars 3
                      clip and this channel in the Cars 2 clip it was ported
                      from (matched across all 1522 C3 / 2305 C2 clips)
  rule                derived from the rig header layout (the rule that
                      reproduced all 25 graph-verified face ports)
Edit PORT_MAP, or pass --map FILE, if a mapping turns out to be wrong.
"""

import argparse
import base64
import json
import os
import struct
import sys
from collections import Counter, OrderedDict

# ---------------------------------------------------------------------------
# Port map: Cars 3 port -> (Cars 2 template, Cars 2 channel index, name, tier)
# ---------------------------------------------------------------------------

PORT_MAP = {
      1: ("car_body",  0, "BackEnd", "verified-curve"),
      2: ("car_body",  1, "BackTires", "rule"),
     12: ("car_body",  2, "BLeft_bttm", "rule"),
     14: ("car_body",  3, "BLeft_Center", "rule"),
     16: ("car_body",  4, "Body2", "rule"),
     17: ("car_body",  5, "Body3", "rule"),
     18: ("car_body",  6, "Body_Baxle", "rule"),
     20: ("car_body",  7, "Body", "rule"),
     22: ("car_body",  8, "BRight_bttm", "rule"),
     24: ("car_body",  9, "BRight_Center", "rule"),
     25: ("car_facial",  0, "BrowCenter", "verified-graph"),
     26: ("car_facial",  1, "BrowCornerLeft", "verified-graph"),
     27: ("car_facial",  2, "BrowCornerRight", "verified-graph"),
     28: ("car_facial",  3, "BrowMidLeft", "verified-graph"),
     29: ("car_facial",  4, "BrowMidRight", "verified-graph"),
     30: ("car_facial",  5, "CheekLowerLeft", "verified-graph"),
     31: ("car_facial",  6, "CheekLowerRight", "verified-graph"),
     32: ("car_facial",  7, "CheekUpperLeft", "verified-graph"),
     33: ("car_facial",  8, "CheekUpperRight", "verified-graph"),
     49: ("car_facial",  9, "Eyes_Ctrl/BTM_CNTR_Notch", "verified-order"),
     50: ("car_facial", 10, "Eyes_Ctrl/BTM_L_Bow", "verified-order"),
     51: ("car_facial", 11, "Eyes_Ctrl/BTM_L_Bow_Angle", "verified-order"),
     52: ("car_facial", 12, "Eyes_Ctrl/BTM_L_Bow_InOut", "verified-order"),
     53: ("car_facial", 13, "Eyes_Ctrl/BTM_L_Edge", "verified-order"),
     54: ("car_facial", 14, "Eyes_Ctrl/BTM_L_Edge_Angle", "verified-order"),
     55: ("car_facial", 15, "Eyes_Ctrl/BTM_L_Notch", "verified-order"),
     56: ("car_facial", 16, "Eyes_Ctrl/BTM_L_Notch_Angle", "verified-order"),
     57: ("car_facial", 17, "Eyes_Ctrl/BTM_R_Bow", "verified-order"),
     58: ("car_facial", 18, "Eyes_Ctrl/BTM_R_Bow_Angle", "verified-order"),
     59: ("car_facial", 19, "Eyes_Ctrl/BTM_R_Bow_InOut", "verified-order"),
     60: ("car_facial", 20, "Eyes_Ctrl/BTM_R_Edge", "verified-order"),
     61: ("car_facial", 21, "Eyes_Ctrl/BTM_R_Edge_Angle", "verified-order"),
     62: ("car_facial", 22, "Eyes_Ctrl/BTM_R_Notch", "verified-order"),
     63: ("car_facial", 23, "Eyes_Ctrl/BTM_R_Notch_Angle", "verified-order"),
     64: ("car_facial", 24, "Eyes_Ctrl/EyeLeft", "verified-order"),
     65: ("car_facial", 25, "Eyes_Ctrl/EyeLeftVertical", "verified-order"),
     66: ("car_facial", 26, "Eyes_Ctrl/EyeRight", "verified-order"),
     67: ("car_facial", 29, "Eyes_Ctrl/TOP_CNTR_Notch", "verified-order"),
     68: ("car_facial", 30, "Eyes_Ctrl/TOP_L_Bow", "verified-order"),
     69: ("car_facial", 31, "Eyes_Ctrl/TOP_L_Bow_Angle", "verified-order"),
     70: ("car_facial", 32, "Eyes_Ctrl/TOP_L_Bow_InOut", "verified-order"),
     71: ("car_facial", 33, "Eyes_Ctrl/TOP_L_Edge", "verified-order"),
     72: ("car_facial", 34, "Eyes_Ctrl/TOP_L_Edge_Angle", "verified-order"),
     73: ("car_facial", 35, "Eyes_Ctrl/TOP_L_Notch", "verified-order"),
     74: ("car_facial", 36, "Eyes_Ctrl/TOP_L_Notch_Angle", "verified-order"),
     75: ("car_facial", 37, "Eyes_Ctrl/TOP_L_Notch_InOut", "verified-order"),
     76: ("car_facial", 38, "Eyes_Ctrl/TOP_R_Bow", "verified-order"),
     77: ("car_facial", 39, "Eyes_Ctrl/TOP_R_Bow_Angle", "verified-order"),
     78: ("car_facial", 40, "Eyes_Ctrl/TOP_R_Bow_InOut", "verified-order"),
     79: ("car_facial", 41, "Eyes_Ctrl/TOP_R_Edge", "verified-order"),
     80: ("car_facial", 42, "Eyes_Ctrl/TOP_R_Edge_Angle", "verified-order"),
     81: ("car_facial", 43, "Eyes_Ctrl/TOP_R_Notch", "verified-order"),
     82: ("car_facial", 44, "Eyes_Ctrl/TOP_R_Notch_Angle", "verified-order"),
     83: ("car_facial", 45, "Eyes_Ctrl/TOP_R_Notch_InOut", "verified-order"),
     84: ("car_facial", 46, "Eyes", "verified-curve"),
     86: ("car_body", 10, "FLeft_bttm", "rule"),
     88: ("car_body", 11, "FLeft_Center", "rule"),
     92: ("car_body", 12, "FRight_bttm", "rule"),
     94: ("car_body", 13, "FRight_Center", "rule"),
     95: ("car_body", 14, "FrontEnd", "rule"),
     96: ("car_body", 15, "FrontTires", "rule"),
    107: ("car_body", 20, "FX_Marker_Exhaust_Left1", "rule"),
    108: ("car_body", 21, "FX_Marker_Exhaust_Left2", "rule"),
    109: ("car_body", 22, "FX_Marker_Exhaust_Right1", "rule"),
    110: ("car_body", 23, "FX_Marker_Exhaust_Right2", "rule"),
    111: ("car_body", 24, "FX_Marker_Headlight_Left", "rule"),
    112: ("car_body", 25, "FX_Marker_Headlight_Right", "rule"),
    113: ("car_body", 26, "FX_Marker_MachineGun_Left", "rule"),
    114: ("car_body", 27, "FX_Marker_MachineGun_Right", "rule"),
    123: ("car_body", 29, "FX_Marker_SpareTire", "rule"),
    129: ("car_facial", 47, "Jaw", "verified-graph"),
    131: ("car_facial", 48, "LipLowerCenter", "verified-graph"),
    132: ("car_facial", 49, "LipLowerCornerLeft", "verified-graph"),
    133: ("car_facial", 50, "LipLowerCornerRight", "verified-graph"),
    134: ("car_facial", 51, "LipLowerMidLeft", "verified-graph"),
    135: ("car_facial", 52, "LipLowerMidRight", "verified-graph"),
    136: ("car_facial", 53, "LipUpperCenter", "verified-graph"),
    137: ("car_facial", 54, "LipUpperCornerLeft", "verified-graph"),
    138: ("car_facial", 55, "LipUpperCornerRight", "verified-graph"),
    139: ("car_facial", 56, "LipUpperMidLeft", "verified-graph"),
    140: ("car_facial", 57, "LipUpperMidRight", "verified-graph"),
    147: ("car_body", 31, "Main_Baxle", "rule"),
    163: ("car_body", 32, "Main", "verified-empirical"),
    164: ("car_body", 33, "MainOrient", "rule"),
    165: ("car_facial", 58, "Mouth_Ctrl/Mth_Corner_L", "verified-curve"),
    166: ("car_facial", 59, "Mouth_Ctrl/Mth_Corner_R", "verified-curve"),
    167: ("car_facial", 60, "Mouth_Ctrl/Mth_Expand_L", "verified-curve"),
    168: ("car_facial", 61, "Mouth_Ctrl/Mth_Expand_R", "verified-curve"),
    169: ("car_facial", 62, "Mouth_Ctrl/Mth_Offset", "verified-curve"),
    195: ("car_body", 34, "TCable_Pulley", "verified-curve"),
    196: ("car_facial", 63, "TeethLower", "verified-graph"),
    197: ("car_facial", 64, "TeethUpper", "verified-graph"),
    200: ("car_facial", 65, "Tongue1", "verified-graph"),
    201: ("car_facial", 66, "Tongue2", "verified-graph"),
    202: ("car_facial", 67, "TongueRoot", "verified-graph"),
    203: ("car_body", 35, "TowCrane", "verified-curve"),
    204: ("car_body", 36, "TowCraneBase", "verified-curve"),
}

# Cars 3 ports that fan out to several Cars 2 channels. Port 199 is Cars 3's
# single Tires_Ctrl; the Cars 2 clips it was ported from key FrontTires and
# BackTires with the same curve (57 + 9 curve matches, all 2-wheel clips).
# A copy is only emitted for a target the clip does not already drive
# through its own port (96 / 2), so explicit tire animation always wins.
MULTI_PORT_MAP = {
    199: [("car_body", 15, "FrontTires", "verified-curve"),
          ("car_body",  1, "BackTires", "verified-curve")],
}

# Cars 3 ports whose name is known but which only exist in a Cars 2 template
# the game-mode split does not use. The 16 visemes follow Mth_Offset in the
# sorted port list (ports 171-186, vsm_AA..vsm_Y including AW). Only the
# frontend template has any of them (vsm_AA, vsm_IH, vsm_M).
NAMED_ONLY_PORTS = {
    171: "Mouth_Ctrl/vsm_AA", 172: "Mouth_Ctrl/vsm_AH", 173: "Mouth_Ctrl/vsm_AO",
    174: "Mouth_Ctrl/vsm_AW", 175: "Mouth_Ctrl/vsm_CH", 176: "Mouth_Ctrl/vsm_EH",
    177: "Mouth_Ctrl/vsm_ER", 178: "Mouth_Ctrl/vsm_EY", 179: "Mouth_Ctrl/vsm_F",
    180: "Mouth_Ctrl/vsm_IH", 181: "Mouth_Ctrl/vsm_L", 182: "Mouth_Ctrl/vsm_M",
    183: "Mouth_Ctrl/vsm_N", 184: "Mouth_Ctrl/vsm_OY", 185: "Mouth_Ctrl/vsm_W",
    186: "Mouth_Ctrl/vsm_Y",
}

# Wheel roll for the frontend. Cars 2 turns all four wheels through two rig
# floats, Main_Ctrl/RotateFront and Main_Ctrl/RotateRear (DEGREES; the rig's
# internal spin clip is 360 long, and its select_idle keys +18 deg for +0.125
# of forward travel on a 0.44 wheel). Cars 3 keeps the same two floats
# (ports 157/158, next to each other in the sorted Main_Ctrl attribute run,
# same units) but actually animates per-wheel "Spin" floats instead (ports
# 13/23/87/93, RADIANS: shrd_podium_tirespin_additive is exactly 6*pi/s, and
# mcqueen_fe_select_idle rolls -2.13 rad for -0.95 of travel).
# Only the frontend template routes these: in game, RotateFront/RotateRear
# are blended from the Choreography interface only (see the .bent Blenders),
# which is where the game drives the rolling wheels.
RAD_TO_DEG = 57.29577951308232
FRONTEND_EXTRA_PORTS = {
    # port: [(name, tier, value scale, fallback)]  fallback = only if not already written
    157: [("Main_Ctrl/RotateFront", "rule", 1.0, False)],
    158: [("Main_Ctrl/RotateRear", "rule", 1.0, False)],
    87:  [("Main_Ctrl/RotateFront", "verified-units", RAD_TO_DEG, True)],   # FLeft spin
    13:  [("Main_Ctrl/RotateRear", "verified-units", RAD_TO_DEG, True)],    # BLeft spin
}
VALUE_SCALE = {(p, n): k for p, es in FRONTEND_EXTRA_PORTS.items() for n, _t, k, _f in es if k != 1.0}
FALLBACK_TARGETS = {(p, n) for p, es in FRONTEND_EXTRA_PORTS.items() for n, _t, _k, f in es if f}


# Cars 2 output lists per connection template, captured from the running game
# (identical to io_cars2_anim.BUILTIN_CHANNEL_MAPS). Every template except
# car_body/car_facial is reached by NAME: a Cars 3 port whose Cars 2 name
# appears in the chosen template's list is written there.
CARS2_CHANNEL_MAPS = {
    "car_body": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "FLeft_bttm", 11: "FLeft_Center",
        12: "FRight_bttm", 13: "FRight_Center", 14: "FrontEnd", 15: "FrontTires",
        16: "FX_Marker_Battery_Center", 17: "FX_Marker_Battery_Left",
        18: "FX_Marker_Battery_Right", 19: "FX_Marker_Bumper",
        20: "FX_Marker_Exhaust_Left1", 21: "FX_Marker_Exhaust_Left2",
        22: "FX_Marker_Exhaust_Right1", 23: "FX_Marker_Exhaust_Right2",
        24: "FX_Marker_Headlight_Left", 25: "FX_Marker_Headlight_Right",
        26: "FX_Marker_MachineGun_Left", 27: "FX_Marker_MachineGun_Right",
        28: "FX_Marker_SkateJack", 29: "FX_Marker_SpareTire",
        30: "FX_Marker_WGPCam", 31: "Main_Baxle", 32: "Main", 33: "MainOrient",
        34: "TCable_Pulley", 35: "TowCrane", 36: "TowCraneBase",
    },
    "car_bodyblend": {
        0: "BackTires", 1: "BLeft_bttm", 2: "BLeft_Center", 3: "Body2",
        4: "Body3", 5: "Body", 6: "BRight_bttm", 7: "BRight_Center",
        8: "FLeft_bttm", 9: "FLeft_Center", 10: "FRight_bttm",
        11: "FRight_Center", 12: "FrontEnd", 13: "FrontTires", 14: "Main",
        15: "MainOrient",
    },
    "car_choblender": {
        0: "Body3", 1: "MainOrient",
    },
    "car_choreography": {
        0: "KinematicState", 2: "BackEnd", 3: "BackTires", 4: "BLeft_bttm",
        5: "BLeft_Center", 6: "Body2", 7: "Body3", 8: "Body_Baxle", 9: "Body",
        10: "BRight_bttm", 11: "BRight_Center", 12: "BrowCenter",
        13: "BrowCornerLeft", 14: "BrowCornerRight", 15: "BrowMidLeft",
        16: "BrowMidRight", 17: "CheekLowerLeft", 18: "CheekLowerRight",
        19: "CheekUpperLeft", 20: "CheekUpperRight", 21: "ControlHandLeft",
        22: "ControlHandRight", 23: "Eyes_Ctrl/BTM_CNTR_Notch",
        24: "Eyes_Ctrl/BTM_L_Bow", 25: "Eyes_Ctrl/BTM_L_Bow_Angle",
        26: "Eyes_Ctrl/BTM_L_Bow_InOut", 27: "Eyes_Ctrl/BTM_L_Edge",
        28: "Eyes_Ctrl/BTM_L_Edge_Angle", 29: "Eyes_Ctrl/BTM_L_Notch",
        30: "Eyes_Ctrl/BTM_L_Notch_Angle", 31: "Eyes_Ctrl/BTM_R_Bow",
        32: "Eyes_Ctrl/BTM_R_Bow_Angle", 33: "Eyes_Ctrl/BTM_R_Bow_InOut",
        34: "Eyes_Ctrl/BTM_R_Edge", 35: "Eyes_Ctrl/BTM_R_Edge_Angle",
        36: "Eyes_Ctrl/BTM_R_Notch", 37: "Eyes_Ctrl/BTM_R_Notch_Angle",
        38: "Eyes_Ctrl/EyeLeft", 39: "Eyes_Ctrl/EyeLeftVertical",
        40: "Eyes_Ctrl/EyeRight", 41: "Eyes_Ctrl/Monocle",
        42: "Eyes_Ctrl/MonocleInOut", 43: "Eyes_Ctrl/TOP_CNTR_Notch",
        44: "Eyes_Ctrl/TOP_L_Bow", 45: "Eyes_Ctrl/TOP_L_Bow_Angle",
        46: "Eyes_Ctrl/TOP_L_Bow_InOut", 47: "Eyes_Ctrl/TOP_L_Edge",
        48: "Eyes_Ctrl/TOP_L_Edge_Angle", 49: "Eyes_Ctrl/TOP_L_Notch",
        50: "Eyes_Ctrl/TOP_L_Notch_Angle", 51: "Eyes_Ctrl/TOP_L_Notch_InOut",
        52: "Eyes_Ctrl/TOP_R_Bow", 53: "Eyes_Ctrl/TOP_R_Bow_Angle",
        54: "Eyes_Ctrl/TOP_R_Bow_InOut", 55: "Eyes_Ctrl/TOP_R_Edge",
        56: "Eyes_Ctrl/TOP_R_Edge_Angle", 57: "Eyes_Ctrl/TOP_R_Notch",
        58: "Eyes_Ctrl/TOP_R_Notch_Angle", 59: "Eyes_Ctrl/TOP_R_Notch_InOut",
        60: "Eyes", 61: "Eyes_Frame_Proxy", 62: "FLeft_bttm", 63: "FLeft_Center",
        64: "FRight_bttm", 65: "FRight_Center", 66: "FrontEnd", 67: "FrontTires",
        68: "Jaw", 69: "LipLowerCenter", 70: "LipLowerCornerLeft",
        71: "LipLowerCornerRight", 72: "LipLowerMidLeft", 73: "LipLowerMidRight",
        74: "LipUpperCenter", 75: "LipUpperCornerLeft",
        76: "LipUpperCornerRight", 77: "LipUpperMidLeft", 78: "LipUpperMidRight",
        79: "Main_Baxle", 80: "Main_Ctrl/RotateFront",
        81: "Main_Ctrl/RotateRear", 82: "Main", 83: "MainOrient",
        84: "Mouth_Ctrl/Mth_Corner_L", 85: "Mouth_Ctrl/Mth_Corner_R",
        86: "Mouth_Ctrl/Mth_Expand_L", 87: "Mouth_Ctrl/Mth_Expand_R",
        88: "Mouth_Ctrl/Mth_Offest", 89: "Mouth_Ctrl/Mth_Offset",
        90: "Mouth_Ctrl/vsm_AA", 91: "Mouth_Ctrl/vsm_AH",
        92: "Mouth_Ctrl/vsm_AO", 93: "Mouth_Ctrl/vsm_CH",
        94: "Mouth_Ctrl/vsm_EH", 95: "Mouth_Ctrl/vsm_ER",
        96: "Mouth_Ctrl/vsm_EY", 97: "Mouth_Ctrl/vsm_F", 98: "Mouth_Ctrl/vsm_IH",
        99: "Mouth_Ctrl/vsm_L", 100: "Mouth_Ctrl/vsm_M", 101: "Mouth_Ctrl/vsm_N",
        102: "Mouth_Ctrl/vsm_OY", 103: "Mouth_Ctrl/vsm_W",
        104: "Mouth_Ctrl/vsm_Y", 105: "TCable_Pulley", 106: "TeethLower",
        107: "TeethUpper", 108: "Tongue1", 109: "Tongue2", 110: "TongueRoot",
        111: "TowCrane", 112: "TowCraneBase",
    },
    "car_eyelids": {  # same output list as car_facial; only the .bent filter masks differ
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_eyes": {
        0: "Eyes_Ctrl/EyeLeft", 1: "Eyes_Ctrl/EyeLeftVertical",
        2: "Eyes_Ctrl/EyeRight", 3: "Eyes",
    },
    "car_facial": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Eyes_Ctrl/BTM_CNTR_Notch", 10: "Eyes_Ctrl/BTM_L_Bow",
        11: "Eyes_Ctrl/BTM_L_Bow_Angle", 12: "Eyes_Ctrl/BTM_L_Bow_InOut",
        13: "Eyes_Ctrl/BTM_L_Edge", 14: "Eyes_Ctrl/BTM_L_Edge_Angle",
        15: "Eyes_Ctrl/BTM_L_Notch", 16: "Eyes_Ctrl/BTM_L_Notch_Angle",
        17: "Eyes_Ctrl/BTM_R_Bow", 18: "Eyes_Ctrl/BTM_R_Bow_Angle",
        19: "Eyes_Ctrl/BTM_R_Bow_InOut", 20: "Eyes_Ctrl/BTM_R_Edge",
        21: "Eyes_Ctrl/BTM_R_Edge_Angle", 22: "Eyes_Ctrl/BTM_R_Notch",
        23: "Eyes_Ctrl/BTM_R_Notch_Angle", 24: "Eyes_Ctrl/EyeLeft",
        25: "Eyes_Ctrl/EyeLeftVertical", 26: "Eyes_Ctrl/EyeRight",
        27: "Eyes_Ctrl/Monocle", 28: "Eyes_Ctrl/MonocleInOut",
        29: "Eyes_Ctrl/TOP_CNTR_Notch", 30: "Eyes_Ctrl/TOP_L_Bow",
        31: "Eyes_Ctrl/TOP_L_Bow_Angle", 32: "Eyes_Ctrl/TOP_L_Bow_InOut",
        33: "Eyes_Ctrl/TOP_L_Edge", 34: "Eyes_Ctrl/TOP_L_Edge_Angle",
        35: "Eyes_Ctrl/TOP_L_Notch", 36: "Eyes_Ctrl/TOP_L_Notch_Angle",
        37: "Eyes_Ctrl/TOP_L_Notch_InOut", 38: "Eyes_Ctrl/TOP_R_Bow",
        39: "Eyes_Ctrl/TOP_R_Bow_Angle", 40: "Eyes_Ctrl/TOP_R_Bow_InOut",
        41: "Eyes_Ctrl/TOP_R_Edge", 42: "Eyes_Ctrl/TOP_R_Edge_Angle",
        43: "Eyes_Ctrl/TOP_R_Notch", 44: "Eyes_Ctrl/TOP_R_Notch_Angle",
        45: "Eyes_Ctrl/TOP_R_Notch_InOut", 46: "Eyes", 47: "Jaw",
        48: "LipLowerCenter", 49: "LipLowerCornerLeft",
        50: "LipLowerCornerRight", 51: "LipLowerMidLeft", 52: "LipLowerMidRight",
        53: "LipUpperCenter", 54: "LipUpperCornerLeft",
        55: "LipUpperCornerRight", 56: "LipUpperMidLeft", 57: "LipUpperMidRight",
        58: "Mouth_Ctrl/Mth_Corner_L", 59: "Mouth_Ctrl/Mth_Corner_R",
        60: "Mouth_Ctrl/Mth_Expand_L", 61: "Mouth_Ctrl/Mth_Expand_R",
        62: "Mouth_Ctrl/Mth_Offset", 63: "TeethLower", 64: "TeethUpper",
        65: "Tongue1", 66: "Tongue2", 67: "TongueRoot",
    },
    "car_frontend_frontend": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body_Baxle", 7: "Body", 8: "BRight_bttm",
        9: "BRight_Center", 10: "BrowCenter", 11: "BrowCornerLeft",
        12: "BrowCornerRight", 13: "BrowMidLeft", 14: "BrowMidRight",
        15: "CheekLowerLeft", 16: "CheekLowerRight", 17: "CheekUpperLeft",
        18: "CheekUpperRight", 19: "ControlArmLeft", 20: "ControlArmRight",
        21: "ControlBrowCenter", 22: "ControlBrowCornerLeft",
        23: "ControlBrowCornerRight", 24: "ControlBrowMidLeft",
        25: "ControlBrowMidRight", 26: "ControlHandLeft", 27: "ControlHandRight",
        28: "ControlStacheLeft", 29: "ControlStacheRight",
        30: "Eyes_Ctrl/BTM_CNTR_Notch", 31: "Eyes_Ctrl/BTM_L_Bow",
        32: "Eyes_Ctrl/BTM_L_Bow_Angle", 33: "Eyes_Ctrl/BTM_L_Bow_InOut",
        34: "Eyes_Ctrl/BTM_L_Edge", 35: "Eyes_Ctrl/BTM_L_Edge_Angle",
        36: "Eyes_Ctrl/BTM_L_Notch", 37: "Eyes_Ctrl/BTM_L_Notch_Angle",
        38: "Eyes_Ctrl/BTM_R_Bow", 39: "Eyes_Ctrl/BTM_R_Bow_Angle",
        40: "Eyes_Ctrl/BTM_R_Bow_InOut", 41: "Eyes_Ctrl/BTM_R_Edge",
        42: "Eyes_Ctrl/BTM_R_Edge_Angle", 43: "Eyes_Ctrl/BTM_R_Notch",
        44: "Eyes_Ctrl/BTM_R_Notch_Angle", 45: "Eyes_Ctrl/EyeLeft",
        46: "Eyes_Ctrl/EyeLeftVertical", 47: "Eyes_Ctrl/EyeRight",
        48: "Eyes_Ctrl/Monocle", 49: "Eyes_Ctrl/MonocleInOut",
        50: "Eyes_Ctrl/TOP_CNTR_Notch", 51: "Eyes_Ctrl/TOP_L_Bow",
        52: "Eyes_Ctrl/TOP_L_Bow_Angle", 53: "Eyes_Ctrl/TOP_L_Bow_InOut",
        54: "Eyes_Ctrl/TOP_L_Edge", 55: "Eyes_Ctrl/TOP_L_Edge_Angle",
        56: "Eyes_Ctrl/TOP_L_Notch", 57: "Eyes_Ctrl/TOP_L_Notch_Angle",
        58: "Eyes_Ctrl/TOP_L_Notch_InOut", 59: "Eyes_Ctrl/TOP_R_Bow",
        60: "Eyes_Ctrl/TOP_R_Bow_Angle", 61: "Eyes_Ctrl/TOP_R_Bow_InOut",
        62: "Eyes_Ctrl/TOP_R_Edge", 63: "Eyes_Ctrl/TOP_R_Edge_Angle",
        64: "Eyes_Ctrl/TOP_R_Notch", 65: "Eyes_Ctrl/TOP_R_Notch_Angle",
        66: "Eyes_Ctrl/TOP_R_Notch_InOut", 67: "Eyes", 68: "FLeft_bttm",
        69: "FLeft_Center", 70: "FRight_bttm", 71: "FRight_Center",
        72: "FrontEnd", 73: "FrontTires", 74: "Jaw", 75: "LipLowerCenter",
        76: "LipLowerCornerLeft", 77: "LipLowerCornerRight",
        78: "LipLowerMidLeft", 79: "LipLowerMidRight", 80: "LipUpperCenter",
        81: "LipUpperCornerLeft", 82: "LipUpperCornerRight",
        83: "LipUpperMidLeft", 84: "LipUpperMidRight", 85: "Main_Baxle",
        86: "Main_Ctrl/RotateFront", 87: "Main_Ctrl/RotateRear", 88: "Main",
        89: "Main_CtrlGrp", 90: "MainOrient", 91: "Mouth_Ctrl/Mth_Corner_L",
        92: "Mouth_Ctrl/Mth_Corner_R", 93: "Mouth_Ctrl/Mth_Expand_L",
        94: "Mouth_Ctrl/Mth_Expand_R", 95: "Mouth_Ctrl/Mth_Offest",
        96: "Mouth_Ctrl/Mth_Offset", 97: "Mouth_Ctrl/vsm_AA",
        98: "Mouth_Ctrl/vsm_IH", 99: "Mouth_Ctrl/vsm_M",
        100: "SideMirrorMountLeft", 101: "SideMirrorMountRight",
        102: "TCable_Pulley", 103: "TeethLower", 104: "TeethUpper",
        105: "Tongue1", 106: "Tongue2", 107: "TongueRoot", 108: "TowCrane",
        109: "TowCraneBase",
    },
    "car_frontend_mouth": {
        0: "BrowCenter", 1: "BrowCornerLeft", 2: "BrowCornerRight",
        3: "BrowMidLeft", 4: "BrowMidRight", 5: "CheekLowerLeft",
        6: "CheekLowerRight", 7: "CheekUpperLeft", 8: "CheekUpperRight",
        9: "Jaw", 10: "LipLowerCenter", 11: "LipLowerCornerLeft",
        12: "LipLowerCornerRight", 13: "LipLowerMidLeft", 14: "LipLowerMidRight",
        15: "LipUpperCenter", 16: "LipUpperCornerLeft",
        17: "LipUpperCornerRight", 18: "LipUpperMidLeft", 19: "LipUpperMidRight",
        20: "Mouth_Ctrl/Mth_Corner_L", 21: "Mouth_Ctrl/Mth_Corner_R",
        22: "Mouth_Ctrl/Mth_Expand_L", 23: "Mouth_Ctrl/Mth_Expand_R",
        24: "Mouth_Ctrl/Mth_Offest", 25: "Mouth_Ctrl/Mth_Offset",
        26: "Mouth_Ctrl/vsm_AA", 27: "Mouth_Ctrl/vsm_IH", 28: "Mouth_Ctrl/vsm_M",
        29: "TeethLower", 30: "TeethUpper", 31: "Tongue1", 32: "Tongue2",
        33: "TongueRoot",
    },
    "car_leanclipset": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "ControlLeftBarrels", 10: "ControlRightBarrels", 11: "FLeft_bttm",
        12: "FLeft_Center", 13: "FRight_bttm", 14: "FRight_Center",
        15: "FrontEnd", 16: "FrontTires", 17: "Main", 18: "MainOrient",
        19: "TowCraneBase",
    },
    "car_mouth": {
        0: "CheekLowerLeft", 1: "CheekLowerRight", 2: "CheekUpperLeft",
        3: "CheekUpperRight", 4: "Jaw", 5: "LipLowerCenter",
        6: "LipLowerCornerLeft", 7: "LipLowerCornerRight", 8: "LipLowerMidLeft",
        9: "LipLowerMidRight", 10: "LipUpperCenter", 11: "LipUpperCornerLeft",
        12: "LipUpperCornerRight", 13: "LipUpperMidLeft", 14: "LipUpperMidRight",
        15: "Mouth_Ctrl/Mth_Corner_L", 16: "Mouth_Ctrl/Mth_Corner_R",
        17: "Mouth_Ctrl/Mth_Expand_L", 18: "Mouth_Ctrl/Mth_Expand_R",
        19: "Mouth_Ctrl/Mth_Offset", 20: "TeethLower", 21: "TeethUpper",
        22: "Tongue1", 23: "Tongue2", 24: "TongueRoot",
    },
    "car_rattles": {
        0: "BackEnd", 1: "BackTires", 2: "BLeft_bttm", 3: "BLeft_Center",
        4: "Body2", 5: "Body3", 6: "Body", 7: "BRight_bttm", 8: "BRight_Center",
        9: "BrowCenter", 10: "BrowCornerLeft", 11: "BrowCornerRight",
        12: "BrowMidLeft", 13: "BrowMidRight", 14: "CheekLowerLeft",
        15: "CheekLowerRight", 16: "CheekUpperLeft", 17: "CheekUpperRight",
        18: "FLeft_bttm", 19: "FLeft_Center", 20: "FRight_bttm",
        21: "FRight_Center", 22: "FrontEnd", 23: "FrontTires", 24: "Jaw",
        25: "LipLowerCenter", 26: "LipLowerCornerLeft",
        27: "LipLowerCornerRight", 28: "LipLowerMidLeft", 29: "LipLowerMidRight",
        30: "LipUpperCenter", 31: "LipUpperCornerLeft",
        32: "LipUpperCornerRight", 33: "LipUpperMidLeft", 34: "LipUpperMidRight",
        35: "Main_Ctrl/RotateFront", 36: "Main_Ctrl/RotateRear", 37: "Main",
        38: "MainOrient", 39: "TeethLower", 40: "TeethUpper", 41: "Tongue1",
        42: "Tongue2", 43: "TongueRoot",
    },
}


FRONTEND_TEMPLATE = "car_frontend_frontend"
GAME_TEMPLATES = ("car_body", "car_facial")
MODE_TEMPLATES = {"game": GAME_TEMPLATES, "frontend": (FRONTEND_TEMPLATE,)}


def template_ct(template):
    folder = "car_frontend" if template.startswith("car_frontend_") else "car"
    return "characters/%s/%s.ct" % (folder, template)


def check_port_map():
    for port, (t, i, n, _tier) in PORT_MAP.items():
        assert CARS2_CHANNEL_MAPS[t][i] == n, (port, t, i, n)
    for port, entries in MULTI_PORT_MAP.items():
        for t, i, n, _tier in entries:
            assert CARS2_CHANNEL_MAPS[t][i] == n, (port, t, i, n)


def retarget_portmap(portmap, templates):
    """Re-target a game port map onto the given Cars 2 templates by name.

    Returns {port: [(template, index, name, tier), ...]}; one port can land
    in several templates (a clip for both the Body and Rattles clip sets).
    Ports whose name exists in none of the templates are left out."""
    index = {t: {n: i for i, n in CARS2_CHANNEL_MAPS[t].items()} for t in templates}
    out = {}
    named = {p: [(None, None, n, "rule")] for p, n in NAMED_ONLY_PORTS.items()}
    for source in (portmap, named):
        for port, entries in source.items():
            for _t, _i, name, tier in entries:
                for t in templates:
                    i = index[t].get(name)
                    if i is None:
                        continue
                    lst = out.setdefault(port, [])
                    if (t, i, name, tier) not in lst and not any(e[0] == t and e[1] == i for e in lst):
                        lst.append((t, i, name, tier))
    if FRONTEND_TEMPLATE in templates:
        fe = index[FRONTEND_TEMPLATE]
        for port, extras in FRONTEND_EXTRA_PORTS.items():
            for name, tier, _k, _f in extras:
                lst = out.setdefault(port, [])
                if not any(e[0] == FRONTEND_TEMPLATE and e[2] == name for e in lst):
                    lst.append((FRONTEND_TEMPLATE, fe[name], name, tier))
    return out


# Channels that keep scale ops in the default --scale=face mode. These are the
# Cars 2 face bones, whose scale is exercised by the shipped select_idle clip.
FACE_TEMPLATE = "car_facial"
FACE_NAMES = {v[2] for v in PORT_MAP.values() if v[0] == "car_facial"} | set(NAMED_ONLY_PORTS.values())


CUSTOM_OP_CARS3 = 10
CUSTOM_OP_CARS2 = 9
SCALE_OPS = (5, 6, 7, 8)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def operand_from_buffer(sub_word, k):
    """True if operand k of a sub-op is a buffer slot (else a pool index)."""
    return ((sub_word >> (4 - 2 * k)) & 3) == 1


def n_operands(sub_word):
    return 1 if (sub_word & 0xF000) in (0x0000, 0x3000) else 3


def buffer_span(sub_word, k):
    """How many consecutive buffer slots operand k covers."""
    if (sub_word & 0xF000) in (0x0000, 0x3000):
        return {0: 3, 4: 4, 5: 3}.get(sub_word & 0xF, 1)
    return 1


def is_field_op(sub_word):
    return (sub_word & 0xF000) == 0x0000


def field_op(sub_word):
    return sub_word & 0xF


def command_channels(cmd_bytes):
    return (struct.unpack_from("<I", cmd_bytes, 0)[0] & 0xFFC00) >> 10


# ---------------------------------------------------------------------------
# ClipDataBlock container (identical in both games)
# ---------------------------------------------------------------------------

def split_block(block):
    flags, duration, n_binds, n_cmds, pool_off = struct.unpack_from("<IfHHI", block, 0)
    offsets = list(struct.unpack_from("<%dI" % (n_binds + n_cmds), block, 16))
    bounds = sorted(offsets) + [pool_off]

    def end_of(o):
        return min(x for x in bounds if x > o)

    return {
        "flags": flags,
        "duration": duration,
        "binds": [block[o:end_of(o)] for o in offsets[:n_binds]],
        "commands": [block[o:end_of(o)] for o in offsets[n_binds:]],
        "pool": block[pool_off:],
    }


def join_block(flags, duration, binds, commands, pool):
    cursor = 16 + 4 * (len(binds) + len(commands))
    offsets = []
    for chunk in list(binds) + list(commands):
        offsets.append(cursor)
        cursor += len(chunk)
    out = bytearray(struct.pack("<IfHHI", flags, duration, len(binds), len(commands), cursor))
    for o in offsets:
        out += struct.pack("<I", o)
    for chunk in list(binds) + list(commands):
        out += chunk
    return bytes(out + pool)


# ---------------------------------------------------------------------------
# Cars 3 bind block reader
# ---------------------------------------------------------------------------

def parse_bind_c3(b):
    """Return dict(cmds, animated, static); records are (port, [(sub, [ops])])."""
    n_cmd = u16(b, 2)
    cmds = [u16(b, 4 + 2 * i) for i in range(n_cmd)]
    p = 4 + 2 * n_cmd
    static_off, n_anim = u16(b, p), u16(b, p + 2)
    p += 4
    heads = [u16(b, p + 2 * i) for i in range(n_anim)]
    p += 2 * n_anim
    animated = []
    for h in heads:
        port, nsub = h & 0x1FFF, h >> 13
        subs = []
        for _ in range(nsub):
            sw = u16(b, p)
            p += 2
            k = n_operands(sw)
            subs.append((sw, [u16(b, p + 2 * i) for i in range(k)]))
            p += 2 * k
        animated.append((port, subs))
    q = static_off
    n_static = u16(b, q)
    q += 2
    sheads = [u16(b, q + 2 * i) for i in range(n_static)]
    q += 2 * n_static
    static = []
    for h in sheads:
        port, nsub = h & 0x1FFF, h >> 13
        subs = []
        for _ in range(nsub):
            subs.append((u16(b, q), [u16(b, q + 2)]))     # (op, [pool slot])
            q += 4
        static.append((port, subs))
    return {"lead": u16(b, 0), "cmds": cmds, "animated": animated, "static": static}


# ---------------------------------------------------------------------------
# Cars 2 bind block reader / writer
# ---------------------------------------------------------------------------

def parse_bind_c2(b):
    n_cmd = u16(b, 2)
    cmds = [u16(b, 4 + 2 * i) for i in range(n_cmd)]
    base = 4 + 2 * n_cmd
    static_off, n_anim = u16(b, base), u16(b, base + 2)

    def walk(p, count):
        records = []
        for _ in range(count):
            word, target = u16(b, p), u16(b, p + 2)
            p += 4
            subs = []
            for _ in range(word & 0xF):
                sw = u16(b, p)
                k = n_operands(sw)
                subs.append((sw, [u16(b, p + 2 + 2 * i) for i in range(k)]))
                p += 2 + 2 * k
            records.append((target, subs))
        return records

    animated = walk(base + 4, n_anim)
    static = walk(static_off + 2, u16(b, static_off)) if static_off else []
    return {"lead": u16(b, 0), "cmds": cmds, "animated": animated,
            "static": static, "has_static_section": bool(static_off)}


FLOAT_CHANNEL_FLAG = 0x20


def record_word(subs):
    """Record header word: sub-op count in the low nibble, plus bit 5 on
    float-typed channels. In every Cars 2 clip checked (235 records) that bit
    is set exactly when the record carries a custom-float op and never on a
    bone transform, so it is derived from the content rather than stored."""
    word = len(subs)
    if any(is_field_op(sw) and field_op(sw) == CUSTOM_OP_CARS2 for sw, _ in subs):
        word |= FLOAT_CHANNEL_FLAG
    return word


def write_bind_c2(lead, cmds, animated, static, static_section=True):
    out = bytearray(struct.pack("<HH", lead, len(cmds)))
    for c in cmds:
        out += struct.pack("<H", c)
    body = bytearray()
    for target, subs in animated:
        body += struct.pack("<HH", record_word(subs), target)
        for sw, ops in subs:
            body += struct.pack("<H", sw)
            for o in ops:
                body += struct.pack("<H", o)
    static_off = len(out) + 4 + len(body) if static_section else 0
    out += struct.pack("<HH", static_off, len(animated)) + body
    if static_section:
        out += struct.pack("<H", len(static))
        for target, subs in static:
            out += struct.pack("<HH", record_word(subs), target)
            for sw, ops in subs:
                out += struct.pack("<H", sw)
                for o in ops:
                    out += struct.pack("<H", o)
    return bytes(out)


def pad_bind_region(binds):
    """The exporter aligns the END of the bind region to 4 bytes, not each
    bind (two 90-byte binds in emo_frown have no padding at all)."""
    binds = list(binds)
    if binds:
        binds[-1] += b"\xee" * (-sum(len(b) for b in binds) % 4)
    return binds


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.mapped = Counter()          # (template, name) -> records
        self.dropped_ports = Counter()   # port -> records
        self.dropped_scale = 0
        self.shadowed = Counter()        # (port, name) -> fan-out copies skipped
        self.dropped_rest = 0            # rest-pose static sub-ops removed
        self.notes = []

    def merge(self, other):
        self.mapped.update(other.mapped)
        self.dropped_ports.update(other.dropped_ports)
        self.dropped_scale += other.dropped_scale
        self.shadowed.update(other.shadowed)
        self.dropped_rest += other.dropped_rest
        self.notes += other.notes


def convert_sub_word(sw):
    if is_field_op(sw) and field_op(sw) == CUSTOM_OP_CARS3:
        return (sw & ~0xF) | CUSTOM_OP_CARS2
    return sw


def scale_allowed(mode, name):
    return mode == "keep" or (mode == "face" and name in FACE_NAMES)


REST_EPS = 1e-5


def static_is_rest(op, slot, pool):
    """True if a static sub-op only restates the rig's rest pose: zero
    translation, identity rotation, unit scale, or a 0.0 float.

    The Cars 3 exporter writes one of these for EVERY channel a clip does not
    animate (79k of 112k static sub-ops across all clips); the Cars 2 exporter
    writes them almost never (23 of 40k). Keeping them makes every clip claim
    channels it does not animate, which overrides whatever else drives them,
    e.g. the wheel roll the game feeds through FrontTires/BackTires."""
    f = op & 0xF
    n = {0: 3, 4: 4, 5: 3}.get(f, 1)
    if 4 * (slot + n) > len(pool):
        return False
    v = struct.unpack_from("<%df" % n, pool, 4 * slot)
    if f == 4:
        return abs(abs(v[3]) - 1.0) < REST_EPS and all(abs(x) < REST_EPS for x in v[:3])
    if f in SCALE_OPS:
        return all(abs(x - 1.0) < REST_EPS for x in v)
    return all(abs(x) < REST_EPS for x in v)


def route_records(bind, portmap, scale_mode, report, templates, pool=b"", keep_rest=False):
    """Split one Cars 3 bind into {template: (animated, static)}, keeping
    the ORIGINAL slot numbering for now.

    portmap values are lists of (template, index, name, tier). Ports listed
    in MULTI_PORT_MAP (and FALLBACK_TARGETS entries) are fan-outs: a copy is
    written only to targets that no ordinary port of this clip already
    drives, and each target is written once per record stream. Records are
    (index, subs, value_scale)."""
    routed = OrderedDict((t, ([], [])) for t in templates)
    for kind, records in (("animated", bind["animated"]), ("static", bind["static"])):
        def fallback(port, name):
            return port in MULTI_PORT_MAP or (port, name) in FALLBACK_TARGETS
        direct = set()
        for port, subs in records:
            for e in portmap.get(port, ()):
                if not fallback(port, e[2]):
                    if kind == "static" and not keep_rest and all(
                            static_is_rest(sw, ops[0], pool) for sw, ops in subs):
                        continue
                    direct.add((e[0], e[1]))
        written = set()
        # ordinary ports first so they claim their targets before fan-outs
        ordered = sorted(records, key=lambda r: all(fallback(r[0], e[2]) for e in portmap.get(r[0], ())) and bool(portmap.get(r[0])))
        for port, subs in ordered:
            entries = portmap.get(port)
            if not entries:
                report.dropped_ports[port] += 1
                continue
            if kind == "static" and not keep_rest:
                live = [(sw, ops) for sw, ops in subs if not static_is_rest(sw, ops[0], pool)]
                report.dropped_rest += len(subs) - len(live)
                subs = live
                if not subs:
                    continue
            for template, index, name, _tier in entries:
                if (template, index) in written or (fallback(port, name) and (template, index) in direct):
                    report.shadowed[(port, name)] += 1
                    continue
                keep = []
                for sw, ops in subs:
                    if is_field_op(sw) and field_op(sw) in SCALE_OPS \
                            and not scale_allowed(scale_mode, name):
                        report.dropped_scale += 1
                        continue
                    keep.append((convert_sub_word(sw), ops))
                if not keep:
                    continue
                written.add((template, index))
                report.mapped[(template, name)] += 1
                routed[template][0 if kind == "animated" else 1].append(
                    (index, keep, VALUE_SCALE.get((port, name), 1.0)))
    return routed


# ---------------------------------------------------------------------------
# Value scaling (unit conversion) for routed records
# ---------------------------------------------------------------------------

def scale_command(cmd_bytes, k):
    """Copy of a curve command with every value multiplied by k."""
    b = bytearray(cmd_bytes)
    w = struct.unpack_from("<I", b, 0)[0]
    n_ch, n_keys, op = (w & 0xFFC00) >> 10, w >> 20, (w & 0x3C0) >> 6
    if op == 0:
        # per channel: preSlope, postSlope, bias, scale (i16 samples untouched)
        for c in range(n_ch):
            vals = struct.unpack_from("<4f", b, 12 + 16 * c)
            struct.pack_into("<4f", b, 12 + 16 * c, *[x * k for x in vals])
    elif op in (4, 6):
        for c in range(n_ch):
            vals = struct.unpack_from("<2f", b, 12 + 8 * c)
            struct.pack_into("<2f", b, 12 + 8 * c, *[x * k for x in vals])
        kt = 12 + n_ch * 8
        sm = kt + n_keys * 2 + (2 if n_keys & 1 else 0) if op == 4 else kt + n_keys * 4
        for i in range(n_keys * n_ch * 4):
            v = struct.unpack_from("<f", b, sm + 4 * i)[0]
            struct.pack_into("<f", b, sm + 4 * i, v * k)
    else:
        raise ValueError("cannot scale curve opcode %d" % op)
    return bytes(b)


def apply_value_scales(routed, bind_cmds, commands, cmd_nch, pool):
    """Resolve records carrying a value scale != 1: animated operands are
    re-pointed at scaled copies of their commands (appended to `commands`
    and to that template's command list), static operands at scaled floats
    appended to the pool. Returns (routed without scales, per-template
    command lists, new pool)."""
    pool = bytearray(pool)
    base, pos = {}, 0
    for ci in bind_cmds:
        base[ci] = pos
        pos += cmd_nch[ci]
    total = pos
    slot_owner = {}
    for ci in bind_cmds:
        for j in range(cmd_nch[ci]):
            slot_owner[base[ci] + j] = (ci, j)
    out, cmd_lists = OrderedDict(), OrderedDict()
    for template, (animated, static) in routed.items():
        clist = list(bind_cmds)
        extra_pos = total
        clones = {}
        anim2 = []
        for index, subs, k in animated:
            if k != 1.0:
                subs2 = []
                for sw, ops in subs:
                    ops = list(ops)
                    for q in range(len(ops)):
                        if operand_from_buffer(sw, q):
                            ci, j = slot_owner[ops[q]]
                            key = (ci, k)
                            if key not in clones:
                                commands.append(scale_command(commands[ci], k))
                                cmd_nch.append(cmd_nch[ci])
                                clones[key] = (len(commands) - 1, extra_pos)
                                clist.append(len(commands) - 1)
                                extra_pos += cmd_nch[ci]
                            ops[q] = clones[key][1] + j
                    subs2.append((sw, ops))
                subs = subs2
            anim2.append((index, subs))
        static2 = []
        for index, subs, k in static:
            if k != 1.0:
                subs2 = []
                for sw, ops in subs:
                    v = struct.unpack_from("<f", pool, 4 * ops[0])[0]
                    subs2.append((sw, [len(pool) // 4]))
                    pool += struct.pack("<f", v * k)
                subs = subs2
            static2.append((index, subs))
        out[template] = (anim2, static2)
        cmd_lists[template] = clist
    return out, cmd_lists, bytes(pool)


def finalize_template_bind(cmd_list, cmd_nch, animated, static):
    """Keep only the commands this bind reads, renumber its buffer slots.

    Returns (kept_command_indices, lead, animated', static')."""
    base, pos = {}, 0
    for ci in cmd_list:
        base[ci] = pos
        pos += cmd_nch[ci]
    total = pos

    used = set()
    for _target, subs in animated:
        for sw, ops in subs:
            for k in range(len(ops)):
                if operand_from_buffer(sw, k):
                    used.update(range(ops[k], ops[k] + buffer_span(sw, k)))
    bad = [s for s in used if s >= total]
    if bad:
        raise ValueError("record reads buffer slot %d but the bind only has %d" % (bad[0], total))

    kept = [ci for ci in cmd_list
            if any(s in used for s in range(base[ci], base[ci] + cmd_nch[ci]))]
    new_slot, pos = {}, 0
    for ci in kept:
        for j in range(cmd_nch[ci]):
            new_slot[base[ci] + j] = pos + j
        pos += cmd_nch[ci]

    def remap(sw, ops):
        out = list(ops)
        for k in range(len(ops)):
            if operand_from_buffer(sw, k):
                span = buffer_span(sw, k)
                first = new_slot[ops[k]]
                for j in range(1, span):
                    if new_slot[ops[k] + j] != first + j:
                        raise ValueError("slot span split across commands")
                out[k] = first
        return out

    animated2 = [(t, [(sw, remap(sw, ops)) for sw, ops in subs]) for t, subs in animated]
    return kept, pos, animated2, static


def convert_block(block, portmap, scale_mode="face", templates=GAME_TEMPLATES, keep_rest=False):
    clip = split_block(block)
    report = Report()
    commands = list(clip["commands"])
    cmd_nch = [command_channels(c) for c in commands]
    pool = clip["pool"]

    per_template = OrderedDict((t, []) for t in templates)     # -> list of finalized binds
    for raw in clip["binds"]:
        bind = parse_bind_c3(raw)
        routed = route_records(bind, portmap, scale_mode, report, templates, pool, keep_rest)
        routed, cmd_lists, pool = apply_value_scales(routed, bind["cmds"], commands, cmd_nch, pool)
        for template, (animated, static) in routed.items():
            if not animated and not static:
                continue
            kept, lead, animated2, static2 = finalize_template_bind(
                cmd_lists[template], cmd_nch, animated, static)
            per_template[template].append((kept, lead, animated2, static2))

    # Merge multiple Cars 3 binds that land on one template (normally there is one).
    out_binds = []          # (template, kept, lead, animated, static)
    for template, items in per_template.items():
        if len(items) > 1:
            report.notes.append("%s: %d source binds; only the first is kept" % (template, len(items)))
        if items:
            out_binds.append((template,) + items[0])

    used_cmds = []
    for _t, kept, _l, _a, _s in out_binds:
        for ci in kept:
            if ci not in used_cmds:
                used_cmds.append(ci)
    new_index = {ci: i for i, ci in enumerate(used_cmds)}

    bind_blobs, meta = [], []
    for template, kept, lead, animated, static in out_binds:
        bind_blobs.append(write_bind_c2(lead, [new_index[c] for c in kept], animated, static))
        meta.append(template)
    out_commands = [commands[ci] for ci in used_cmds]
    block2 = join_block(clip["flags"], clip["duration"], pad_bind_region(bind_blobs),
                        out_commands, pool)
    return block2, meta, report


def verify_block(block2, templates):
    """Re-read the block we just wrote and check every invariant the game relies on."""
    clip = split_block(block2)
    nch = [command_channels(c) for c in clip["commands"]]
    problems = []
    for template, raw in zip(templates, clip["binds"]):
        bind = parse_bind_c2(raw)
        slots = sum(nch[c] for c in bind["cmds"])
        if bind["lead"] != slots:
            problems.append("%s: slot count %d != %d" % (template, bind["lead"], slots))
        for target, subs in bind["animated"]:
            for sw, ops in subs:
                for k in range(len(ops)):
                    if operand_from_buffer(sw, k) and ops[k] + buffer_span(sw, k) > slots:
                        problems.append("%s: target %d reads past the buffer" % (template, target))
        if any(c >= len(clip["commands"]) for c in bind["cmds"]):
            problems.append("%s: command index out of range" % template)
    return problems


# ---------------------------------------------------------------------------
# JSON envelopes
# ---------------------------------------------------------------------------

def b64_unpadded(data):
    return "base64:" + base64.b64encode(data).decode("ascii").rstrip("=")


def b64_decode(text):
    text = text[len("base64:"):] if text.startswith("base64:") else text
    return base64.b64decode(text + "=" * (-len(text) % 4))


def read_cars3_json(path):
    """Read a Cars 3 clip from `offsetting oct decode` JSON or straight from .oct."""
    if path.lower().endswith(".oct"):
        import octread
        doc = json.loads(octread.dumps(octread.load(path)))
    else:
        with open(path) as handle:
            doc = json.load(handle)
    sub = doc["SubNetworkPool"]["SubNetwork#0"]
    if sub.get("Type") != "Clip":
        raise ValueError("not a clip (SubNetwork Type = %r)" % sub.get("Type"))
    nodes = sub["DataNodePool"]
    binds = sub.get("LayerBindPool", {})
    refs = sorted({b.get("ClipDataNodeRef", 0) for b in binds.values()}) or [0]
    if len(refs) != 1:
        raise ValueError("clip uses %d ClipData nodes; only one is supported" % len(refs))
    node = nodes.get("DataNode#%d" % refs[0])
    if not node or "ClipDataBlock" not in node:
        raise ValueError("DataNode#%d has no ClipDataBlock" % refs[0])
    header = sub.get("HeaderStrings") or []
    name = header[0][len("ClipData-"):] if header and header[0].startswith("ClipData-") else \
        os.path.splitext(os.path.basename(path))[0]
    return {
        "name": name,
        "block": b64_decode(node["ClipDataBlock"]),
        "date": doc.get("ExporterDate", "Mar  4 2011"),
        "time": doc.get("ExporterTime", "00:00:00"),
    }


# ---- Cars 3 -> Cars 2 clip NAMES (v2.0.56: here so every path - Import Cars 3
# Clip, Convert Cars 3 Clips, this script's command line, port_car.py - names
# clips the same way). Rules: clip_names.json next to this file. shrd_ ->
# shared_ is BUILT IN as well: every Cars 3 shared clip becomes a Cars 2
# shared_ clip even when the json is missing or broken.
BUILTIN_CLIP_PREFIXES = {"shrd_": "shared_"}
# A character's frontend clip drops the character: <char>_fe_select_idle ->
# select_idle, as Cars 2 names them (select_idle / select_idle2 /
# select_selected in every character's motions). Class clips keep their fe
# (shrd_fe_horn -> shared_fe_horn, like Cars 2's shared_fe_blocker). The two
# Cars 3 spellings Cars 2 doesn't have get the Cars 2 name.
FE_CLIP_NAMES = {"selected": "select_selected", "select_idle1": "select_idle"}


def load_name_map(path=None):
    """{'prefixes': {cars3_start: cars2_start}, 'names': {cars3_name: cars2_name}}
    from clip_names.json (+ 'classes': mack_ -> truck_) + the built-in rules."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_names.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    prefixes = {str(k).lower(): v for k, v in (d.get("prefixes") or {}).items()}
    for cls, chars in (d.get("classes") or {}).items():     # mack_ -> truck_
        for ch in chars or ():
            prefixes.setdefault(str(ch).lower().rstrip("_") + "_", str(cls).rstrip("_") + "_")
    for k, v in BUILTIN_CLIP_PREFIXES.items():
        prefixes.setdefault(k, v)
    keep_fe = {str(k).lower() for k in (d.get("prefixes") or {})} | set(BUILTIN_CLIP_PREFIXES)
    return {"prefixes": prefixes, "keep_fe": keep_fe,
            "names": {str(k).lower(): v for k, v in (d.get("names") or {}).items()}}


def cars2_clip_name(name, rules=None):
    """A Cars 3 clip name as Cars 2 names that kind of clip: shrd_ -> shared_,
    trk_ -> truck_, <char>_fe_select_idle -> select_idle ... (whole-name
    rules first, then the frontend rule, then the longest matching
    start, any case: SHRD_BM_InAir -> shared_BM_InAir). Unchanged when no
    rule fits."""
    rules = rules or load_name_map()
    low = name.lower()
    if low in rules["names"]:
        return rules["names"][low]
    i = low.find("_fe_")            # mcqueen_fe_select_idle -> select_idle
    if i > 0 and low[:i + 1] not in rules.get("keep_fe", ()) and len(low) > i + 4:
        rest = name[i + 4:]
        return FE_CLIP_NAMES.get(rest.lower(), rest)
    for pre in sorted(rules["prefixes"], key=len, reverse=True):
        if low.startswith(pre):
            return rules["prefixes"][pre] + name[len(pre):]
    return name


def build_cars2_json(name, block2, templates, date, time):
    layer_binds = OrderedDict()
    for i, template in enumerate(templates):
        layer_binds["LayerBind#%d" % i] = OrderedDict([
            ("Name", template),
            ("ConnectionTemplate", template_ct(template)),
            ("ClipDataNodeRef", 0),
            ("ClipDataCommandBlockOffset", i),
        ])
    return OrderedDict([
        ("SubNetworkPool", OrderedDict([
            ("SubNetwork#0", OrderedDict([
                ("Type", "Clip"),
                ("HeaderStrings", ["ClipData-%s" % name]),
                ("HeaderStringIndices", [0]),
                ("HeaderLayout", [0, 1]),
                ("DataNodePool", OrderedDict([
                    ("DataNode#0", OrderedDict([
                        ("Header", 1),
                        ("Type", "ClipData"),
                        ("Static", 1),
                        ("ClipDataBlock", b64_unpadded(block2)),
                    ])),
                ])),
                ("LayerBindPool", layer_binds),
            ])),
        ])),
        ("ExporterDate", date),
        ("ExporterTime", time),
    ])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_portmap(extra_path=None, templates=GAME_TEMPLATES):
    """{port: [(template, index, name, tier), ...]} for "game" or "frontend".

    --map overrides use the game templates; in frontend mode they are
    re-targeted by name like every other entry."""
    portmap = {p: [v] for p, v in PORT_MAP.items()}
    for p, entries in MULTI_PORT_MAP.items():
        portmap[p] = list(entries)
    if extra_path:
        with open(extra_path) as handle:
            extra = json.load(handle)
        for port, value in extra.items():
            if value is None:
                portmap.pop(int(port), None)
            else:
                template, index = value[0], int(value[1])
                name = value[2] if len(value) > 2 else "custom"
                portmap[int(port)] = [(template, index, name, "user")]
    if tuple(templates) != GAME_TEMPLATES:
        portmap = retarget_portmap(portmap, templates)
    return portmap


def parse_mode(mode):
    """'game', 'frontend', or a comma list of Cars 2 template names."""
    if mode in MODE_TEMPLATES:
        return MODE_TEMPLATES[mode]
    templates = tuple(t.strip() for t in mode.split(",") if t.strip())
    unknown = [t for t in templates if t not in CARS2_CHANNEL_MAPS]
    if unknown or not templates:
        raise ValueError("unknown template(s) %s; known: %s" % (
            ",".join(unknown), ", ".join(sorted(CARS2_CHANNEL_MAPS))))
    return templates


FRONTEND_CLIP_HINTS = ("_fe_", "fe_select", "select_idle", "select_selected", "frontend")


def detect_mode(src, clip_name):
    text = (os.path.basename(src) + " " + clip_name).lower()
    return "frontend" if any(h in text for h in FRONTEND_CLIP_HINTS) else "game"


def convert_file(src, dst, extra_map, scale_mode, mode="auto", keep_rest=False, cars2_names=True):
    """Returns (name written into the clip, templates, report); cars2_names:
    the clip gets its Cars 2 name (shrd_ -> shared_ ...)."""
    clip = read_cars3_json(src)
    if mode == "auto":
        mode = detect_mode(src, clip["name"])
    wanted = parse_mode(mode)
    portmap = load_portmap(extra_map, wanted)
    block2, templates, report = convert_block(clip["block"], portmap, scale_mode, wanted, keep_rest)
    if not templates:
        raise ValueError("no channel in this clip has a Cars 2 equivalent")
    problems = verify_block(block2, templates)
    if problems:
        raise ValueError("; ".join(problems))
    name = cars2_clip_name(clip["name"]) if cars2_names else clip["name"]
    doc = build_cars2_json(name, block2, templates, clip["date"], clip["time"])
    with open(dst, "w") as handle:
        json.dump(doc, handle, indent=1)
    return name, templates, report


def describe(report, portmap_names):
    parts = []
    by_t = Counter()
    for (template, _name), n in report.mapped.items():
        by_t[template] += n
    parts.append(", ".join("%d %s" % (n, t) for t, n in sorted(by_t.items())) or "nothing mapped")
    if report.dropped_ports:
        parts.append("dropped %d unmapped records (ports %s)" % (
            sum(report.dropped_ports.values()),
            ",".join(str(p) for p in sorted(report.dropped_ports))))
    if report.dropped_scale:
        parts.append("dropped %d scale ops" % report.dropped_scale)
    if report.dropped_rest:
        parts.append("%d rest-pose statics removed" % report.dropped_rest)
    if report.shadowed:
        parts.append("kept explicit %s over fan-out" % ",".join(
            sorted({n for (_p, n) in report.shadowed})))
    parts += report.notes
    return "; ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="*", help="Cars 3 clips: .oct or offsetting JSON (or directories of them)")
    ap.add_argument("-o", "--output", help="output file (single input only)")
    ap.add_argument("--outdir", help="output directory for batch conversion")
    ap.add_argument("--scale", choices=("face", "keep", "drop"), default="face",
                    help="scale ops: keep on face bones only (default), everywhere, or nowhere")
    ap.add_argument("--map", help="JSON overrides: {\"163\": [\"car_body\", 32, \"Main\"], \"15\": null}")
    ap.add_argument("--dump-map", metavar="FILE", help="write the port map (with evidence tiers) and exit")
    ap.add_argument("--mode", default="auto",
                    help="game = car_body + car_facial; frontend = car_frontend_frontend; "
                         "or a comma list of Cars 2 templates (e.g. car_body,car_rattles). "
                         "auto (default) picks frontend for *_fe_* / select_* clips, else game")
    ap.add_argument("--keep-rest-statics", action="store_true",
                    help="keep Cars 3 rest-pose static records (off by default; they override "
                         "channels the clip does not animate, e.g. game-driven wheel roll)")
    ap.add_argument("--cars3-names", action="store_true",
                    help="keep the Cars 3 clip names (default: Cars 2 names, shrd_ -> shared_ ...; "
                         "rules in clip_names.json)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    check_port_map()
    portmaps = {m: load_portmap(args.map, MODE_TEMPLATES[m]) for m in ("game", "frontend")}
    if args.dump_map:
        rows = [{"mode": m, "cars3_port": p, "template": v[0], "index": v[1],
                 "cars2_name": v[2], "evidence": v[3]}
                for m in ("game", "frontend")
                for p, vs in sorted(portmaps[m].items()) for v in vs]
        with open(args.dump_map, "w") as handle:
            json.dump(rows, handle, indent=1)
        print("wrote %d port mappings to %s" % (len(rows), args.dump_map))
        return 0
    if not args.inputs:
        ap.error("no input files")

    files = []
    for path in args.inputs:
        if os.path.isdir(path):
            files += sorted(os.path.join(path, f) for f in os.listdir(path)
                            if f.endswith((".json", ".oct")))
        else:
            files.append(path)
    if args.output and len(files) != 1:
        ap.error("-o needs exactly one input; use --outdir for several")

    ok = failed = 0
    total = Report()
    for src in files:
        if args.output:
            dst = args.output
        else:
            folder = args.outdir or os.path.dirname(src) or "."
            os.makedirs(folder, exist_ok=True)
            stem = os.path.splitext(os.path.basename(src))[0]
            if not args.cars3_names:                # shrd_x.oct -> shared_x.cars2.json
                stem = cars2_clip_name(stem)
            dst = os.path.join(folder, stem + ".cars2.json")
        try:
            name, templates, report = convert_file(src, dst, args.map, args.scale, args.mode,
                                                   args.keep_rest_statics, not args.cars3_names)
        except (ValueError, KeyError, struct.error, json.JSONDecodeError) as exc:
            failed += 1
            print("SKIP %s: %s" % (os.path.basename(src), exc), file=sys.stderr)
            continue
        ok += 1
        total.merge(report)
        if not args.quiet:
            print("%-44s %s | %s" % (os.path.basename(src), "+".join(templates), describe(report, None)))
    print("\nconverted %d, skipped %d" % (ok, failed))
    if total.dropped_ports and not args.quiet:
        print("unmapped ports seen (records dropped): " + ", ".join(
            "%d x%d" % (p, n) for p, n in sorted(total.dropped_ports.items())))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
