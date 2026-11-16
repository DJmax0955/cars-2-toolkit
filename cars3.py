"""Import a Cars 3 .oct: characters with the same armature layout as a Cars 2
import, anything else (weapons, props) with its own node tree.

File > Import > Cars 3 Character (.oct; an .oct.json works too). For a character it builds,
from the model file and its <name>_0.vbuf / <name>_0.ibuf:

  * the skin skeleton, with its hierarchy taken from the Cars 3 rig
    (RB_Main -> Main -> Body -> face bones / wheel hubs);
  * the control rig under Main_CtrlGrp, named and structured exactly like a
    Cars 2 import: a Cars 3 node gets the bone name the Cars 2 importer gives
    the same node (FX_Marker_X -> FX_Marker_X_Ctrl, Body_VSM_Proxy ->
    Body_Baxle_Proxy, face X_CtrlGrp -> X_Proxy); Cars 3-only nodes and
    the pass-through groups a Cars 2 import leaves out are left out the same
    way, their children moving up to the nearest kept bone;
  * the FX markers, parented to their frame;
  * the meshes, one object per file primitive (named after its material and
    LOD), weighted to the skin bones.

Coordinates follow the Cars 2 import: bones and meshes in game space (Y up),
the armature object rotated to stand up in Blender - so the plugin's tools
see the same layout as on a Cars 2 character. Textures (colour, normal,
specular, gloss, emission ...) are loaded by cars3_textures with the Cars 2
World Importer's Cars 3 material decoder. The controls are structure only: they
don't drive the skin bones (Cars 3 links them through a different network).
"""
import json
import math
import os
import struct

import bpy
import mathutils
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import c2_model
from . import character as ch

PROP_SOURCE = 'cars2tk_cars3_source'
PROP_PROP_SOURCE = 'cars2tk_cars3_prop'    # non-character .oct (weapons, props)
PROP_CHAINS = 'cars2tk_chains'          # dynamic chains to build on export (JSON list)

# Generated from the Cars 2 importer run on McQueen, Mater, Sheriff, The King,
# Guido and Doc Hudson: which rig nodes become bones, under what name, and
# which bone collection each goes to.
C2_NODE_TO_BONE = {
 "BLeft_Center_Ctrl": "BLeft_Center_Ctrl",
 "BLeft_bttm_Ctrl": "BLeft_bttm_Ctrl",
 "BRight_Center_Ctrl": "BRight_Center_Ctrl",
 "BRight_bttm_Ctrl": "BRight_bttm_Ctrl",
 "BackEnd_Ctrl": "BackEnd_Ctrl",
 "BackTires_Ctrl": "BackTires_Ctrl",
 "Body2_Ctrl": "Body2_Ctrl",
 "Body3_Ctrl": "Body3_Ctrl",
 "Body_Baxle_Ctrl": "Body_Baxle_Ctrl",
 "Body_Ctrl": "Body_Ctrl",
 "Body_Final_Proxy": "Body_Final_Proxy",
 "Body_VSM_Proxy": "Body_Baxle_Proxy",
 "BrowCenter_Ctrl": "BrowCenter_Ctrl",
 "BrowCornerLeft_Ctrl": "BrowCornerLeft_Ctrl",
 "BrowCornerRight_Ctrl": "BrowCornerRight_Ctrl",
 "BrowMidLeft_Ctrl": "BrowMidLeft_Ctrl",
 "BrowMidRight_Ctrl": "BrowMidRight_Ctrl",
 "CableRigBaseCenterProxy": "CableRigBaseCenterProxy",
 "CableRigBaseLeftProxy": "CableRigBaseLeftProxy",
 "CableRigBaseRightProxy": "CableRigBaseRightProxy",
 "CableRigBaseWheelCenterProxy": "CableRigBaseWheelCenterProxy",
 "CableRigBaseWheelCenterProxy1": "CableRigBaseWheelCenterProxy1",
 "CableRigBaseWheelLeftProxy": "CableRigBaseWheelLeftProxy",
 "CableRigBaseWheelLeftProxy1": "CableRigBaseWheelLeftProxy1",
 "CableRigBaseWheelRightProxy": "CableRigBaseWheelRightProxy",
 "CableRigBaseWheelRightProxy1": "CableRigBaseWheelRightProxy1",
 "CableRigEndCenter_Proxy": "CableRigEndCenter_Proxy",
 "CableRigEndCenter_Proxy1": "CableRigEndCenter_Proxy1",
 "CableRigEndLeft_Proxy": "CableRigEndLeft_Proxy",
 "CableRigEndLeft_Proxy1": "CableRigEndLeft_Proxy1",
 "CableRigEndRight_Proxy": "CableRigEndRight_Proxy",
 "CableRigEndRight_Proxy1": "CableRigEndRight_Proxy1",
 "CableRigWheelEndLeft_Proxy": "CableRigWheelEndLeft_Proxy",
 "CableRigWheelEndLeft_Proxy1": "CableRigWheelEndLeft_Proxy1",
 "CableRigWheelEndRight_Proxy": "CableRigWheelEndRight_Proxy",
 "CableRigWheelEndRight_Proxy1": "CableRigWheelEndRight_Proxy1",
 "CheekLowerLeft_Ctrl": "CheekLowerLeft_Ctrl",
 "CheekLowerLeft_Proxy": "CheekLowerLeft_Proxy",
 "CheekLowerRight_Ctrl": "CheekLowerRight_Ctrl",
 "CheekLowerRight_Proxy": "CheekLowerRight_Proxy",
 "CheekUpperLeft_Ctrl": "CheekUpperLeft_Ctrl",
 "CheekUpperLeft_Proxy": "CheekUpperLeft_Proxy",
 "CheekUpperRight_Ctrl": "CheekUpperRight_Ctrl",
 "CheekUpperRight_Proxy": "CheekUpperRight_Proxy",
 "ControlArmLeft": "ControlArmLeft_Ctrl",
 "ControlArmRight": "ControlArmRight_Ctrl",
 "ControlHandLeft": "ControlHandLeft_Ctrl",
 "ControlHandRight": "ControlHandRight_Ctrl",
 "Eyes_Ctrl": "Eyes_Ctrl",
 "Eyes_Frame_Proxy": "Eyes_Frame_Proxy",
 "FLeft_Center_Ctrl": "FLeft_Center_Ctrl",
 "FLeft_bttm_Ctrl": "FLeft_bttm_Ctrl",
 "FRight_Center_Ctrl": "FRight_Center_Ctrl",
 "FRight_bttm_Ctrl": "FRight_bttm_Ctrl",
 "FX_Marker_Battery_Center": "FX_Marker_Battery_Center_Ctrl",
 "FX_Marker_Battery_Left": "FX_Marker_Battery_Left_Ctrl",
 "FX_Marker_Battery_Right": "FX_Marker_Battery_Right_Ctrl",
 "FX_Marker_Bumper": "FX_Marker_Bumper_Ctrl",
 "FX_Marker_Exhaust_Left1": "FX_Marker_Exhaust_Left1_Ctrl",
 "FX_Marker_Exhaust_Left2": "FX_Marker_Exhaust_Left2_Ctrl",
 "FX_Marker_Exhaust_Right1": "FX_Marker_Exhaust_Right1_Ctrl",
 "FX_Marker_Exhaust_Right2": "FX_Marker_Exhaust_Right2_Ctrl",
 "FX_Marker_Headlight_Left": "FX_Marker_Headlight_Left_Ctrl",
 "FX_Marker_Headlight_Right": "FX_Marker_Headlight_Right_Ctrl",
 "FX_Marker_MachineGun_Left": "FX_Marker_MachineGun_Left_Ctrl",
 "FX_Marker_MachineGun_Right": "FX_Marker_MachineGun_Right_Ctrl",
 "FX_Marker_SkateJack": "FX_Marker_SkateJack_Ctrl",
 "FX_Marker_SpareTire": "FX_Marker_SpareTire_Ctrl",
 "FX_Marker_WGPCam": "FX_Marker_WGPCam_Ctrl",
 "FrontEnd_Ctrl": "FrontEnd_Ctrl",
 "FrontTires_Ctrl": "FrontTires_Ctrl",
 "Jaw_Ctrl": "Jaw_Ctrl",
 "Jaw_Proxy": "Jaw_Proxy",
 "LipLowerCenter_Ctrl": "LipLowerCenter_Ctrl",
 "LipLowerCenter_Proxy": "LipLowerCenter_Proxy",
 "LipLowerCornerLeft_Ctrl": "LipLowerCornerLeft_Ctrl",
 "LipLowerCornerLeft_Proxy": "LipLowerCornerLeft_Proxy",
 "LipLowerCornerRight_Ctrl": "LipLowerCornerRight_Ctrl",
 "LipLowerCornerRight_Proxy": "LipLowerCornerRight_Proxy",
 "LipLowerMidLeft_Ctrl": "LipLowerMidLeft_Ctrl",
 "LipLowerMidLeft_Proxy": "LipLowerMidLeft_Proxy",
 "LipLowerMidRight_Ctrl": "LipLowerMidRight_Ctrl",
 "LipLowerMidRight_Proxy": "LipLowerMidRight_Proxy",
 "LipUpperCenter_Ctrl": "LipUpperCenter_Ctrl",
 "LipUpperCenter_Proxy": "LipUpperCenter_Proxy",
 "LipUpperCornerLeft_Ctrl": "LipUpperCornerLeft_Ctrl",
 "LipUpperCornerLeft_Proxy": "LipUpperCornerLeft_Proxy",
 "LipUpperCornerRight_Ctrl": "LipUpperCornerRight_Ctrl",
 "LipUpperCornerRight_Proxy": "LipUpperCornerRight_Proxy",
 "LipUpperMidLeft_Ctrl": "LipUpperMidLeft_Ctrl",
 "LipUpperMidLeft_Proxy": "LipUpperMidLeft_Proxy",
 "LipUpperMidRight_Ctrl": "LipUpperMidRight_Ctrl",
 "LipUpperMidRight_Proxy": "LipUpperMidRight_Proxy",
 "MainOrient_Ctrl": "MainOrient_Ctrl",
 "Main_Baxle_Ctrl": "Main_Baxle_Ctrl",
 "Main_Ctrl": "Main_Ctrl",
 "Main_CtrlGrp": "Main_CtrlGrp",
 "ProxyAimUpCableHingeCenter": "ProxyAimUpCableHingeCenter",
 "ProxyAimUpCableHingeLeft": "ProxyAimUpCableHingeLeft",
 "ProxyAimUpCableHingeRight": "ProxyAimUpCableHingeRight",
 "SideMirrorLeft_Ctrl": "SideMirrorLeft_Ctrl",
 "SideMirrorMountLeft_Ctrl": "SideMirrorMountLeft_Ctrl",
 "SideMirrorMountRight_Ctrl": "SideMirrorMountRight_Ctrl",
 "SideMirrorRight_Ctrl": "SideMirrorRight_Ctrl",
 "TCable_Pulley_Ctrl": "TCable_Pulley_Ctrl",
 "TeethLower_Ctrl": "TeethLower_Ctrl",
 "TeethLower_Proxy": "TeethLower_Proxy",
 "TeethUpper_Ctrl": "TeethUpper_Ctrl",
 "TeethUpper_Proxy": "TeethUpper_Proxy",
 "Tongue1_Ctrl": "Tongue1_Ctrl",
 "Tongue1_Proxy": "Tongue1_Proxy",
 "Tongue2_Ctrl": "Tongue2_Ctrl",
 "Tongue2_Proxy": "Tongue2_Proxy",
 "TongueRoot_Ctrl": "TongueRoot_Ctrl",
 "TongueRoot_Proxy": "TongueRoot_Proxy",
 "TowCraneBase_Ctrl": "TowCraneBase_Ctrl",
 "TowCrane_Ctrl": "TowCrane_Ctrl"
}

C2_BONE_KIND = {
 "BLeft_Center_Ctrl": "control",
 "BLeft_bttm_Ctrl": "control",
 "BRight_Center_Ctrl": "control",
 "BRight_bttm_Ctrl": "control",
 "BackEnd_Ctrl": "control",
 "BackEnd_Offset": "internal",
 "BackTires_Ctrl": "control",
 "Body2_Ctrl": "control",
 "Body3_Ctrl": "control",
 "Body_Baxle_Ctrl": "control",
 "Body_Baxle_Proxy": "internal",
 "Body_Ctrl": "control",
 "Body_Final_Proxy": "control",
 "Body_Follow": "internal",
 "BrowCenter_Ctrl": "control",
 "BrowCornerLeft_Ctrl": "control",
 "BrowCornerRight_Ctrl": "control",
 "BrowMidLeft_Ctrl": "control",
 "BrowMidRight_Ctrl": "control",
 "CableRigBaseCenterProxy": "internal",
 "CableRigBaseLeftProxy": "internal",
 "CableRigBaseRightProxy": "internal",
 "CableRigBaseWheelCenterProxy": "internal",
 "CableRigBaseWheelCenterProxy1": "internal",
 "CableRigBaseWheelLeftProxy": "internal",
 "CableRigBaseWheelLeftProxy1": "internal",
 "CableRigBaseWheelRightProxy": "internal",
 "CableRigBaseWheelRightProxy1": "internal",
 "CableRigEndCenter_Proxy": "internal",
 "CableRigEndCenter_Proxy1": "internal",
 "CableRigEndLeft_Proxy": "internal",
 "CableRigEndLeft_Proxy1": "internal",
 "CableRigEndRight_Proxy": "internal",
 "CableRigEndRight_Proxy1": "internal",
 "CableRigWheelEndLeft_Proxy": "internal",
 "CableRigWheelEndLeft_Proxy1": "internal",
 "CableRigWheelEndRight_Proxy": "internal",
 "CableRigWheelEndRight_Proxy1": "internal",
 "CheekLowerLeft_Ctrl": "control",
 "CheekLowerLeft_Proxy": "internal",
 "CheekLowerRight_Ctrl": "control",
 "CheekLowerRight_Proxy": "internal",
 "CheekUpperLeft_Ctrl": "control",
 "CheekUpperLeft_Proxy": "internal",
 "CheekUpperRight_Ctrl": "control",
 "CheekUpperRight_Proxy": "internal",
 "ControlArmLeft_Ctrl": "control",
 "ControlArmRight_Ctrl": "control",
 "ControlHandLeft_Ctrl": "control",
 "ControlHandRight_Ctrl": "control",
 "Eyes_Ctrl": "control",
 "Eyes_Frame_Proxy": "control",
 "FLeft_Center_Ctrl": "control",
 "FLeft_bttm_Ctrl": "control",
 "FRight_Center_Ctrl": "control",
 "FRight_bttm_Ctrl": "control",
 "FX_Marker_Battery_Center_Ctrl": "fx",
 "FX_Marker_Battery_Left_Ctrl": "fx",
 "FX_Marker_Battery_Right_Ctrl": "fx",
 "FX_Marker_Bumper_Ctrl": "fx",
 "FX_Marker_Exhaust_Left1_Ctrl": "fx",
 "FX_Marker_Exhaust_Left2_Ctrl": "fx",
 "FX_Marker_Exhaust_Right1_Ctrl": "fx",
 "FX_Marker_Exhaust_Right2_Ctrl": "fx",
 "FX_Marker_Headlight_Left_Ctrl": "fx",
 "FX_Marker_Headlight_Right_Ctrl": "fx",
 "FX_Marker_MachineGun_Left_Ctrl": "fx",
 "FX_Marker_MachineGun_Right_Ctrl": "fx",
 "FX_Marker_SkateJack_Ctrl": "fx",
 "FX_Marker_SpareTire_Ctrl": "fx",
 "FX_Marker_WGPCam_Ctrl": "fx",
 "FrontEnd_Ctrl": "control",
 "FrontTires_Ctrl": "control",
 "Jaw_Ctrl": "control",
 "Jaw_Proxy": "internal",
 "LipLowerCenter_Ctrl": "control",
 "LipLowerCenter_Proxy": "internal",
 "LipLowerCornerLeft_Ctrl": "control",
 "LipLowerCornerLeft_Proxy": "internal",
 "LipLowerCornerRight_Ctrl": "control",
 "LipLowerCornerRight_Proxy": "internal",
 "LipLowerMidLeft_Ctrl": "control",
 "LipLowerMidLeft_Proxy": "internal",
 "LipLowerMidRight_Ctrl": "control",
 "LipLowerMidRight_Proxy": "internal",
 "LipUpperCenter_Ctrl": "control",
 "LipUpperCenter_Proxy": "internal",
 "LipUpperCornerLeft_Ctrl": "control",
 "LipUpperCornerLeft_Proxy": "internal",
 "LipUpperCornerRight_Ctrl": "control",
 "LipUpperCornerRight_Proxy": "internal",
 "LipUpperMidLeft_Ctrl": "control",
 "LipUpperMidLeft_Proxy": "internal",
 "LipUpperMidRight_Ctrl": "control",
 "LipUpperMidRight_Proxy": "internal",
 "MainOrient_Ctrl": "control",
 "Main_Baxle_Ctrl": "control",
 "Main_Ctrl": "control",
 "Main_CtrlGrp": "control",
 "ProxyAimUpCableHingeCenter": "internal",
 "ProxyAimUpCableHingeLeft": "internal",
 "ProxyAimUpCableHingeRight": "internal",
 "SideMirrorLeft_Ctrl": "internal",
 "SideMirrorMountLeft_Ctrl": "control",
 "SideMirrorMountRight_Ctrl": "control",
 "SideMirrorRight_Ctrl": "internal",
 "TCable_Pulley_Ctrl": "control",
 "TeethLower_Ctrl": "control",
 "TeethLower_Proxy": "internal",
 "TeethUpper_Ctrl": "control",
 "TeethUpper_Proxy": "internal",
 "TireBLeft_centerJoint_Follow": "internal",
 "TireBRight_centerJoint_Follow": "internal",
 "TireFLeft_centerJoint_Follow": "internal",
 "TireFRight_centerJoint_Follow": "internal",
 "Tongue1_Ctrl": "control",
 "Tongue1_Proxy": "internal",
 "Tongue2_Ctrl": "control",
 "Tongue2_Proxy": "internal",
 "TongueRoot_Ctrl": "control",
 "TongueRoot_Proxy": "internal",
 "TowCraneBase_Ctrl": "control",
 "TowCrane_Ctrl": "control"
}


# The parent each rig bone has in a Cars 2 import (most common across the
# same six characters).
C2_BONE_PARENT = {
 "BLeft_Center_Ctrl": "BLeft_bttm_Ctrl",
 "BLeft_bttm_Ctrl": "BackTires_Ctrl",
 "BRight_Center_Ctrl": "BRight_bttm_Ctrl",
 "BRight_bttm_Ctrl": "BackTires_Ctrl",
 "BackEnd_Ctrl": "Body_Final_Proxy",
 "BackTires_Ctrl": "Main_Baxle_Ctrl",
 "Body2_Ctrl": "Body_Ctrl",
 "Body3_Ctrl": "Body2_Ctrl",
 "Body_Baxle_Ctrl": "Body_Baxle_Proxy",
 "Body_Baxle_Proxy": "Body3_Ctrl",
 "Body_Ctrl": "Main_Baxle_Ctrl",
 "Body_Final_Proxy": "Body_Baxle_Ctrl",
 "BrowCenter_Ctrl": "Body_Final_Proxy",
 "BrowCornerLeft_Ctrl": "Body_Final_Proxy",
 "BrowCornerRight_Ctrl": "Body_Final_Proxy",
 "BrowMidLeft_Ctrl": "Body_Final_Proxy",
 "BrowMidRight_Ctrl": "Body_Final_Proxy",
 "CableRigBaseCenterProxy": "BackEnd_Ctrl",
 "CableRigBaseLeftProxy": "BackEnd_Ctrl",
 "CableRigBaseRightProxy": "BackEnd_Ctrl",
 "CableRigBaseWheelCenterProxy": "CableRigBaseCenterProxy",
 "CableRigBaseWheelCenterProxy1": "CableRigBaseCenterProxy",
 "CableRigBaseWheelLeftProxy": "CableRigBaseLeftProxy",
 "CableRigBaseWheelLeftProxy1": "CableRigBaseLeftProxy",
 "CableRigBaseWheelRightProxy": "CableRigBaseRightProxy",
 "CableRigBaseWheelRightProxy1": "CableRigBaseRightProxy",
 "CableRigEndCenter_Proxy": "TowCrane_Ctrl",
 "CableRigEndCenter_Proxy1": "TowCrane_Ctrl",
 "CableRigEndLeft_Proxy": "TowCrane_Ctrl",
 "CableRigEndLeft_Proxy1": "TowCrane_Ctrl",
 "CableRigEndRight_Proxy": "TowCrane_Ctrl",
 "CableRigEndRight_Proxy1": "TowCrane_Ctrl",
 "CableRigWheelEndLeft_Proxy": "CableRigEndLeft_Proxy",
 "CableRigWheelEndLeft_Proxy1": "CableRigEndLeft_Proxy",
 "CableRigWheelEndRight_Proxy": "CableRigEndRight_Proxy",
 "CableRigWheelEndRight_Proxy1": "CableRigEndRight_Proxy",
 "CheekLowerLeft_Ctrl": "CheekLowerLeft_Proxy",
 "CheekLowerLeft_Proxy": "FrontEnd_Ctrl",
 "CheekLowerRight_Ctrl": "CheekLowerRight_Proxy",
 "CheekLowerRight_Proxy": "FrontEnd_Ctrl",
 "CheekUpperLeft_Ctrl": "CheekUpperLeft_Proxy",
 "CheekUpperLeft_Proxy": "FrontEnd_Ctrl",
 "CheekUpperRight_Ctrl": "CheekUpperRight_Proxy",
 "CheekUpperRight_Proxy": "FrontEnd_Ctrl",
 "ControlArmLeft_Ctrl": "Body_Final_Proxy",
 "ControlArmRight_Ctrl": "Body_Final_Proxy",
 "ControlHandLeft_Ctrl": "ControlArmLeft_Ctrl",
 "ControlHandRight_Ctrl": "ControlArmRight_Ctrl",
 "Eyes_Ctrl": "Eyes_Frame_Proxy",
 "Eyes_Frame_Proxy": "Body_Final_Proxy",
 "FLeft_Center_Ctrl": "FLeft_bttm_Ctrl",
 "FLeft_bttm_Ctrl": "FrontTires_Ctrl",
 "FRight_Center_Ctrl": "FRight_bttm_Ctrl",
 "FRight_bttm_Ctrl": "FrontTires_Ctrl",
 "FX_Marker_Battery_Center_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Battery_Left_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Battery_Right_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Bumper_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Exhaust_Left1_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Exhaust_Left2_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Exhaust_Right1_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Exhaust_Right2_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Headlight_Left_Ctrl": "Body_Final_Proxy",
 "FX_Marker_Headlight_Right_Ctrl": "Body_Final_Proxy",
 "FX_Marker_MachineGun_Left_Ctrl": "Body_Final_Proxy",
 "FX_Marker_MachineGun_Right_Ctrl": "Body_Final_Proxy",
 "FX_Marker_SkateJack_Ctrl": "Body_Final_Proxy",
 "FX_Marker_SpareTire_Ctrl": "Body_Final_Proxy",
 "FX_Marker_WGPCam_Ctrl": "Body_Final_Proxy",
 "FrontEnd_Ctrl": "Body_Final_Proxy",
 "FrontTires_Ctrl": "Main_Baxle_Ctrl",
 "Jaw_Ctrl": "Jaw_Proxy",
 "Jaw_Proxy": "FrontEnd_Ctrl",
 "LipLowerCenter_Ctrl": "LipLowerCenter_Proxy",
 "LipLowerCenter_Proxy": "Jaw_Ctrl",
 "LipLowerCornerLeft_Ctrl": "LipLowerCornerLeft_Proxy",
 "LipLowerCornerLeft_Proxy": "FrontEnd_Ctrl",
 "LipLowerCornerRight_Ctrl": "LipLowerCornerRight_Proxy",
 "LipLowerCornerRight_Proxy": "FrontEnd_Ctrl",
 "LipLowerMidLeft_Ctrl": "LipLowerMidLeft_Proxy",
 "LipLowerMidLeft_Proxy": "Jaw_Ctrl",
 "LipLowerMidRight_Ctrl": "LipLowerMidRight_Proxy",
 "LipLowerMidRight_Proxy": "Jaw_Ctrl",
 "LipUpperCenter_Ctrl": "LipUpperCenter_Proxy",
 "LipUpperCenter_Proxy": "FrontEnd_Ctrl",
 "LipUpperCornerLeft_Ctrl": "LipUpperCornerLeft_Proxy",
 "LipUpperCornerLeft_Proxy": "FrontEnd_Ctrl",
 "LipUpperCornerRight_Ctrl": "LipUpperCornerRight_Proxy",
 "LipUpperCornerRight_Proxy": "FrontEnd_Ctrl",
 "LipUpperMidLeft_Ctrl": "LipUpperMidLeft_Proxy",
 "LipUpperMidLeft_Proxy": "FrontEnd_Ctrl",
 "LipUpperMidRight_Ctrl": "LipUpperMidRight_Proxy",
 "LipUpperMidRight_Proxy": "FrontEnd_Ctrl",
 "MainOrient_Ctrl": "Main_CtrlGrp",
 "Main_Baxle_Ctrl": "Main_Ctrl",
 "Main_Ctrl": "MainOrient_Ctrl",
 "Main_CtrlGrp": None,
 "ProxyAimUpCableHingeCenter": "BackEnd_Ctrl",
 "ProxyAimUpCableHingeLeft": "BackEnd_Ctrl",
 "ProxyAimUpCableHingeRight": "BackEnd_Ctrl",
 "SideMirrorLeft_Ctrl": "SideMirrorMountLeft_Ctrl",
 "SideMirrorMountLeft_Ctrl": "FrontEnd_Ctrl",
 "SideMirrorMountRight_Ctrl": "FrontEnd_Ctrl",
 "SideMirrorRight_Ctrl": "SideMirrorMountRight_Ctrl",
 "TCable_Pulley_Ctrl": "TowCrane_Ctrl",
 "TeethLower_Ctrl": "TeethLower_Proxy",
 "TeethLower_Proxy": "Jaw_Ctrl",
 "TeethUpper_Ctrl": "TeethUpper_Proxy",
 "TeethUpper_Proxy": "FrontEnd_Ctrl",
 "Tongue1_Ctrl": "Tongue1_Proxy",
 "Tongue1_Proxy": "TongueRoot_Ctrl",
 "Tongue2_Ctrl": "Tongue2_Proxy",
 "Tongue2_Proxy": "Tongue1_Ctrl",
 "TongueRoot_Ctrl": "TongueRoot_Proxy",
 "TongueRoot_Proxy": "Jaw_Ctrl",
 "TowCraneBase_Ctrl": "BackEnd_Ctrl",
 "TowCrane_Ctrl": "TowCraneBase_Ctrl"
}

# ------------------------------------------------------------------ reading
def _strip(path):
    base = path
    for suffix in ('.oct.json', '_oct.json', '.json', '.oct'):
        if base.lower().endswith(suffix):
            return base[:-len(suffix)]
    return base


def _group_repeats(v):
    """The binary reader keeps entries that share a name as Key, Key~1,
    Key~2 ... (e.g. BasisConversion); offsetting's JSON has them as one list.
    Bring them into the JSON shape, so both file types read the same."""
    if isinstance(v, list):
        return [_group_repeats(x) for x in v]
    if not isinstance(v, dict):
        return v
    out, groups = {}, {}
    for k, x in v.items():
        base, sep, num = k.partition('~')
        if sep and num.isdigit() and base in v:
            groups.setdefault(base, [v[base]]).append(_group_repeats(x))
        elif k not in out:
            out[k] = _group_repeats(x)
    for base, items in groups.items():
        items[0] = _group_repeats(items[0])
        out[base] = items
    return out


def read_cars3(filepath):
    """-> (data in offsetting's JSON shape, vbuf bytes, ibuf bytes, model name)."""
    if filepath.lower().endswith('.json'):
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        import c2oct
        data = _group_repeats(ch.tree_to_json(c2oct.load(filepath)))
    base = _strip(filepath)
    bufs = []
    for ext in ('_0.vbuf', '_0.ibuf'):
        p = base + ext
        if not os.path.isfile(p):
            raise RuntimeError('%s not found next to the model' % os.path.basename(p))
        with open(p, 'rb') as f:
            bufs.append(f.read())
    return data, bufs[0], bufs[1], os.path.basename(base)


def _pool(p):
    if not p:
        return []
    def num(k):
        tail = k.rsplit('#', 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return [v for _k, v in sorted(p.items(), key=lambda kv: num(kv[0]))]


def _mat_row_major(flat16):
    m = mathutils.Matrix([flat16[0:4], flat16[4:8], flat16[8:12], flat16[12:16]])
    m.transpose()
    return m


def _rot(q):
    x, y, z, w = q[:4]                 # stored conjugated relative to mathutils
    return mathutils.Quaternion((w, -x, -y, -z)).to_matrix().to_4x4()


def _local(data):
    if not isinstance(data, dict):
        return mathutils.Matrix.Identity(4)
    t = data.get('Translation') or (0.0, 0.0, 0.0)
    r = _rot(data['Orientation']) if data.get('Orientation') else mathutils.Matrix.Identity(4)
    s = data.get('Scale', 1.0)
    s = (s, s, s) if isinstance(s, (int, float)) else tuple(s[:3])
    return mathutils.Matrix.Translation(mathutils.Vector(t[:3])) @ r @ \
        mathutils.Matrix.Diagonal((s[0], s[1], s[2], 1.0))


# ------------------------------------------------------------------ skeleton
def mesh_nodes(data):
    return [n for n in data['SceneTreeNodePool'].values()
            if n.get('Type') == 'Geometry' and n.get('Primitives')]


def skin_binds(nodes):
    """{bone: bind matrix (game space)} in first-seen order."""
    out = {}
    for n in nodes:
        infl = n.get('Influences') or {}
        for i in range(len(infl)):
            inf = infl.get('Influence#%d' % i) or {}
            nm = inf.get('Name')
            if nm and nm not in out:
                out[nm] = _mat_row_major(inf['BindPoseSkinLocalToWorldMatrixInverseData']).inverted()
    return out


def read_rig(data, binds, shift=None):
    """The rig's frame tree, FX markers and the controls Cars 3 keeps as data
    inside frames (e.g. McQueen's lip controls in FrontEnd), game space, rig
    offset and ground shift applied."""
    sn = next((s for s in _pool(data.get('SubNetworkPool'))
               if s.get('BasisPool') and s.get('DataNodePool')), None)
    if sn is None:
        return None
    HS, HI, HL = sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout']
    dn = _pool(sn['DataNodePool'])
    bases = _pool(sn['BasisPool'])
    conv = (sn.get('BasisConversionPool') or {}).get('BasisConversion') or []
    if isinstance(conv, dict):
        conv = [conv]                    # a single entry
    owners, cum = [], 2                  # HeaderLayout: (name header, count) laid end to end
    for i in range(0, len(HL) - 1, 2):
        h, c = HL[i], HL[i + 1]
        owners.append((cum, c, HS[HI[h]] if 0 <= h < len(HI) and 0 <= HI[h] < len(HS) else None))
        cum += c

    def owner(header):
        for start, count, name in owners:
            if start <= header < start + count:
                return name
        return None
    parent_dn = {}
    for c in conv:
        b = c['FromBasisRef']
        if 0 <= b < len(bases) and bases[b].get('ParentRef') == c['ToBasisRef']:
            parent_dn[b] = c['DataNodeRef']
    cache = {}

    def world(b, depth=0):
        if b in cache:
            return cache[b]
        p = bases[b].get('ParentRef')
        loc = _local(dn[parent_dn[b]].get('Data')) if b in parent_dn else mathutils.Matrix.Identity(4)
        m = (world(p, depth + 1) @ loc) if p is not None and depth < 256 else loc
        cache[b] = m
        return m
    diffs = [binds[b.get('Name')].translation - world(i).translation
             for i, b in enumerate(bases) if b.get('Name') in binds]
    offset = mathutils.Vector()
    if diffs:
        diffs.sort(key=lambda v: v.length)
        offset = diffs[len(diffs) // 2]
    off = mathutils.Matrix.Translation(offset + (shift or mathutils.Vector()))
    markers, controls = [], []
    frame_names = set(b.get('Name') for b in bases)
    for i, node in enumerate(dn):
        nm = owner(node.get('Header', -1))
        b = node.get('BasisRef')
        if not nm or node.get('Type') != 'Transform' or b is None or not 0 <= b < len(bases):
            continue
        m = off @ world(b) @ _local(node.get('Data'))
        if nm.startswith('FX_'):
            markers.append((nm, b, m))
        elif nm not in frame_names and cars2_name(nm):
            controls.append((nm, b, m))
    # v2.0.36: the look control. Cars 3 keeps it as a Position value in Eyes_Frame_Proxy (no Transform
    # of its own) - the node the eyes' RadialBasisPosition reads. As the Cars 2 rig's Eyes_Ctrl bone, at
    # the frame's axes, its pose location is that value (clips' Eyes channel, eye preview).
    if not any(cars2_name(n) == 'Eyes_Ctrl' for n, _b, _m in controls):
        for p in _pool(sn.get('ProcessorNodePool')):
            refs = p.get('DataNodeRefs') or []
            if p.get('Type') != 'RadialBasisPosition' or not refs or not 0 <= refs[0] < len(dn):
                continue
            node = dn[refs[0]]
            b = node.get('BasisRef')
            if node.get('Type') == 'Position' and isinstance(b, int) and 0 <= b < len(bases):
                pos = node.get('Data') if isinstance(node.get('Data'), list) else [0.0, 0.0, 0.0]
                controls.append(('Eyes_Ctrl', b, off @ world(b) @ mathutils.Matrix.Translation(pos[:3])))
            break
    return {'names': [b.get('Name') for b in bases],
            'parent': [b.get('ParentRef') for b in bases],
            'world': lambda b: off @ world(b),
            'markers': markers,
            'controls': controls}


# Cars 3 node -> the Cars 2 node in the same role, where the names differ.
# Mouth_Ctrl is the face's root control (Cars 2: FrontEnd_Ctrl).
CARS3_ROLE = {'Mouth_Ctrl': 'FrontEnd_Ctrl'}


def cars2_name(raw):
    """The bone name a Cars 2 import gives this rig node, or None if a Cars 2
    import leaves it out."""
    raw = CARS3_ROLE.get(raw, raw)
    if raw in C2_NODE_TO_BONE:
        return C2_NODE_TO_BONE[raw]
    if raw.endswith('_CtrlGrp') and raw[:-len('_CtrlGrp')] + '_Proxy' in C2_BONE_KIND:
        return raw[:-len('_CtrlGrp')] + '_Proxy'            # face part groups
    return None


def _kind(bone):
    k = C2_BONE_KIND.get(bone)
    if k:
        return k
    return 'fx' if bone.startswith('FX_') else ('control' if bone.endswith('_Ctrl') else 'internal')


def _skin_parents(names, rig):
    par = {}
    if rig:
        index = {nm: i for i, nm in enumerate(rig['names']) if nm}
        for nm in names:
            i = index.get(nm)
            p = rig['parent'][i] if i is not None else None
            while p is not None and rig['names'][p] not in names and rig['names'][p] != 'Main':
                p = rig['parent'][p]
            if p is not None:
                par[nm] = rig['names'][p]
    for side in ('FLeft', 'FRight', 'BLeft', 'BRight'):    # spin joints aren't rig frames
        spin = 'Tire%s_SpinJoint' % side
        center = _hub_name(names, side)
        if spin in names and center:
            par.setdefault(spin, center)
    for nm in names:
        if nm != 'Main':
            par.setdefault(nm, 'Body' if nm not in ('Body',) and 'Body' in names
                           and not nm.startswith('Tire') else 'Main')
    return par


def _hub_name(names, side):
    """The wheel hub bone: Tire<side>_centerJoint, or a variant of it (Mater's skin binds
    Tire<side>_centerJointSuspension) - v2.0.51."""
    exact = 'Tire%s_centerJoint' % side
    if exact in names:
        return exact
    return next((n for n in sorted(names) if n.startswith(exact)), None)


def _hub_side(name):
    """'FLeft' for Tire FLeft hub names (any centerJoint variant), else None."""
    for side in ('FLeft', 'FRight', 'BLeft', 'BRight'):
        if name.startswith('Tire%s_centerJoint' % side):
            return side
    return None


MARKER_GROUP = 'FX_Markers'      # without the control rig: the markers' group bone, as the Cars 2 plain import


def build_armature(name, binds, rig, control_rig=True, markers=True):
    """control_rig False: the skin bones only (one plain hierarchy, as the
    Cars 2 import without its control rig); markers then hang under an
    FX_Markers group bone below Body, named FX_Marker_<Name> (v2.0.16).
    markers False: no FX marker bones at all."""
    arm_data = bpy.data.armatures.new(name)
    arm = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    L = 0.05
    skin = dict(binds)
    if 'Main' not in skin:
        skin['Main'] = mathutils.Matrix.Identity(4)
    # Cars 2 hangs the face under a FrontEnd skin bone; a Cars 3 car without
    # one gets a weightless FrontEnd at its face root control (Mouth_Ctrl),
    # so the face bones sit exactly where they do in a Cars 2 import.
    face_root = None
    if 'FrontEnd' not in skin and rig and 'Mouth_Ctrl' in rig['names']:
        loc, rot, _s = rig['world'](rig['names'].index('Mouth_Ctrl')).decompose()
        skin['FrontEnd'] = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        face_root = 'FrontEnd'

    for nm, m in skin.items():
        eb = arm_data.edit_bones.new(nm)
        loc, rot, _s = m.decompose()
        eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, L, 0.0)
        eb.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
    parents = _skin_parents(set(skin) - ({face_root} if face_root else set()), rig)
    if face_root:
        for c, p in list(parents.items()):
            if p == 'Body' and not c.startswith(('Tire', 'BackEnd')):
                parents[c] = face_root          # the face, as in Cars 2
        parents[face_root] = 'Body'
    for c, p in parents.items():
        if c in arm_data.edit_bones and p in arm_data.edit_bones and c != p:
            arm_data.edit_bones[c].parent = arm_data.edit_bones[p]
    rig_bones = {}
    if control_rig and rig and 'Main_CtrlGrp' in rig['names']:
        names, parent = rig['names'], rig['parent']
        kids = {}
        for i, p in enumerate(parent):
            if p is not None:
                kids.setdefault(p, []).append(i)
        stack = [names.index('Main_CtrlGrp')]
        kept_of = {}                   # basis -> bone name of itself or nearest kept ancestor
        while stack:
            i = stack.pop()
            p = parent[i]
            up = kept_of.get(p)
            bn = cars2_name(names[i]) if names[i] != 'Main_CtrlGrp' else 'Main_CtrlGrp'
            if bn and bn not in arm_data.edit_bones:
                loc, rot, _s = rig['world'](i).decompose()
                eb = arm_data.edit_bones.new(bn)
                eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, L, 0.0)
                eb.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
                eb.use_deform = False
                if up:
                    eb.parent = arm_data.edit_bones[up]
                rig_bones[bn] = i
                kept_of[i] = bn
            else:
                kept_of[i] = up
            stack.extend(reversed(kids.get(i, [])))
        def parent_for(bn, b):
            # where a Cars 2 import puts it; else the nearest kept frame; a
            # frame outside the control tree (a skin frame like Body) means
            # Body_Final_Proxy, where Cars 2 hangs the body's markers
            for cand in (C2_BONE_PARENT.get(bn), kept_of.get(b)):
                if cand and cand in arm_data.edit_bones:
                    return cand
            if 'Body_Final_Proxy' in arm_data.edit_bones:
                return 'Body_Final_Proxy'
            return 'Main_CtrlGrp' if 'Main_CtrlGrp' in arm_data.edit_bones else None
        extra = [(C2_NODE_TO_BONE.get(raw, raw + '_Ctrl'), b, m) for raw, b, m in rig['markers']] if markers else []
        # A face control sits on its face bone at rest (as on every Cars 2
        # character: Jaw_Ctrl on Jaw, LipUpperCenter_Ctrl on LipUpperCenter).
        # Use that: some Cars 3 files keep the control's real place only as a
        # computed value (Fritter's lip controls are stored as zeros).
        def on_bone(raw, m):
            bone = raw[:-len('_Ctrl')] if raw.endswith('_Ctrl') else None
            return skin[bone] if bone and bone in skin else m
        extra += [(cars2_name(raw), b, on_bone(raw, m)) for raw, b, m in rig['controls']]
        for bn, b, m in extra:
            if not bn or bn in arm_data.edit_bones:
                continue
            loc, rot, _s = m.decompose()
            eb = arm_data.edit_bones.new(bn)
            eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, L, 0.0)
            eb.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
            eb.use_deform = False
            rig_bones[bn] = b
        for bn, b, _m in extra:                 # parents once all exist
            if bn in rig_bones and arm_data.edit_bones[bn].parent is None:
                up = parent_for(bn, b)
                if up and up != bn:
                    arm_data.edit_bones[bn].parent = arm_data.edit_bones[up]
    plain_markers = []
    if not control_rig and markers and rig and rig.get('markers'):
        # as the Cars 2 import without its control rig: FX_Marker_<Name> bones
        # under an FX_Markers group bone (under Body), FX_Markers collection
        body = arm_data.edit_bones.get('Body') or arm_data.edit_bones.get('Main')
        grp = arm_data.edit_bones.new(MARKER_GROUP)
        grp.head = body.head.copy() if body else (0.0, 0.0, 0.0)
        grp.tail = grp.head + ((body.tail - body.head) if body else mathutils.Vector((0.0, L, 0.0)))
        if body:
            grp.roll = body.roll
        grp.parent = body
        grp.use_deform = False
        plain_markers.append(MARKER_GROUP)
        for raw, _b, m in rig['markers']:
            c2 = C2_NODE_TO_BONE.get(raw, raw + '_Ctrl')
            bn = c2[:-len('_Ctrl')] if c2.endswith('_Ctrl') else c2      # the rig node name: FX_Marker_X
            if bn in arm_data.edit_bones:
                continue
            loc, rot, _s = m.decompose()
            eb = arm_data.edit_bones.new(bn)
            eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, L, 0.0)
            eb.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
            eb.use_deform = False
            eb.parent = grp
            plain_markers.append(bn)
    try:                                # the same bone collections as a Cars 2 import
        colls = {}

        def coll(cname, visible=True):
            if cname not in colls:
                c = arm_data.collections.new(cname)
                c.is_visible = visible
                colls[cname] = c
            return colls[cname]
        for nm in skin:
            coll('Skin').assign(arm_data.edit_bones[nm])
        for bn in rig_bones:
            k = _kind(bn)
            target = {'control': 'Controls', 'fx': 'FX Markers'}.get(k, 'Rig Internals')
            coll(target, k != 'internal').assign(arm_data.edit_bones[bn])
        for bn in plain_markers:
            coll(MARKER_GROUP).assign(arm_data.edit_bones[bn])
    except AttributeError:
        pass
    bpy.ops.object.mode_set(mode='OBJECT')
    for bn, b in rig_bones.items():
        arm_data.bones[bn]['c3_basis'] = b
    return arm, list(skin), len(rig_bones) + max(0, len(plain_markers) - 1)


# ------------------------------------------------------------------ meshes
# True: one Blender object per primitive in the file (Cars 3 models are made of
# many - McQueen has 22: the body, tires, eyes, lights ... each in three LODs,
# plus shadow casters). False: the old behaviour, every primitive that shares a
# material merged into one object.
SPLIT_BY_PRIMITIVE = True

# RenderCaps bits that tell the LOD of a primitive apart (McQueen: 0x20000 on
# the full-detail set, 0x40000 on the next, 0x180000 on the lowest; the vertex
# counts drop in the same order).
_LOD_BITS = ((0x20000, 0), (0x40000, 1), (0x180000, 2))


def _lod(prim):
    caps = prim.get('RenderCaps')
    if not isinstance(caps, int):
        return None
    for bit, lod in _LOD_BITS:
        if caps & bit == bit:
            return lod
    return None


def _streams(prim):
    """Vdata = [stream count, vertex count, (pad, offset, stride) per stream].
    Stream 0: position (4 x int16) + UV (2 x half); on a skinned primitive
    stream 1 holds 4 bone indices + 4 weights. Props carry other streams
    there instead (2 or 3 in all)."""
    vd = prim['Vdata']
    return vd[1], [(vd[3 + 3 * k], vd[4 + 3 * k]) for k in range(vd[0])]


def _decode(vb, ib, data, prim, skinned=True):
    ib_ref, offset, _pad, count = prim['Idata']
    width = data['IndexBufferPool']['IndexBuffer#%d' % ib_ref]['Width']
    idx = struct.unpack_from('<%d%s' % (count, {1: 'B', 2: 'H', 4: 'I'}[width]), ib, offset)
    vcount, streams = _streams(prim)
    off_a, stride_a = streams[0]
    off_b, stride_b = streams[1] if skinned and len(streams) > 1 else (None, 0)
    ub, us = prim['UnitBase'], prim['UnitScale']
    pos, uvs, skin = [], [], []
    for i in range(vcount):
        o = off_a + i * stride_a
        x, y, z, _w = struct.unpack_from('<4h', vb, o)
        pos.append((ub[0] + x / 32767.0 * us[0], ub[1] + y / 32767.0 * us[1], ub[2] + z / 32767.0 * us[2]))
        u, v = struct.unpack_from('<2e', vb, o + 8)
        uvs.append((u, 1.0 - v))
        if off_b is None:
            continue
        ob = off_b + i * stride_b
        bi = struct.unpack_from('<4B', vb, ob)
        bw = struct.unpack_from('<4B', vb, ob + 4)
        pairs = [(bi[k], bw[k]) for k in range(4) if bw[k] > 0] or [(bi[0], 1)]
        t = float(sum(w for _b, w in pairs))
        skin.append([(b, w / t) for b, w in pairs])
    tris = [tuple(idx[k:k + 3]) for k in range(0, len(idx) - 2, 3)]
    return pos, uvs, skin, tris


def _s10(v):
    return (v - 1024 if v >= 512 else v) / 511.0


def _decode_frame(vb, prim):
    """v2.0.39: the game's tangent frame of a skinned primitive - the second stream's bytes 12..15 are the tangent
    as signed 10:10:10 with a 2-bit bitangent sign in the top bits (bytes 8..11 the normal, same packing;
    checked on McQueen's body: normal . face normal median 0.999, tangent . normal 0.000). -> (tangents, signs)
    in game space, or (None, None)."""
    vcount, streams = _streams(prim)
    if len(streams) < 2 or streams[1][1] < 16:
        return None, None
    off_b, stride_b = streams[1]
    tans, signs = [], []
    for i in range(vcount):
        v = struct.unpack_from('<I', vb, off_b + i * stride_b + 12)[0]
        t = (_s10(v & 1023), _s10((v >> 10) & 1023), _s10((v >> 20) & 1023))
        w = (v >> 30) & 3
        tans.append(t)
        signs.append(-1.0 if w >= 2 else 1.0)
    return tans, signs


def ground_shift(data, vb, ib):
    """How far to lift the model so the car stands on the ground (y = 0), as
    a Cars 2 model does - Cars 3 files differ (Brick stands on the ground,
    McQueen is modelled around his axles). The shadow caster doesn't count."""
    mats = _pool(data.get('MaterialPool'))
    low = None
    for node in mesh_nodes(data):
        for prim in node['Primitives'].values():
            try:
                nm = mats[int(prim.get('MaterialReference'))]['Name']
            except (TypeError, ValueError, IndexError, KeyError):
                nm = prim.get('MaterialName', '')
            if 'shadow' in str(nm).lower():
                continue
            pos = _decode(vb, ib, data, prim, skinned=False)[0]
            y = min(p[1] for p in pos) if pos else None
            if y is not None and (low is None or y < low):
                low = y
    return mathutils.Vector((0.0, -low, 0.0)) if low is not None and abs(low) > 1e-4 \
        else mathutils.Vector()


TANGENT_ATTR = 'c3_tangent'
TSIGN_ATTR = 'c3_tangent_sign'


def build_meshes(data, vb, ib, arm, shift=None, lods=None):
    """Meshes in game space, weighted. One object per file primitive, named
    '<material>_LOD<n>' (Blender adds .001 ... if a name repeats) - or, with
    SPLIT_BY_PRIMITIVE off, one object per material as a Cars 2 import.
    lods: the LOD sets to build ({0, 1, 2}; None = all). Parts without a LOD
    are always built (v2.0.16)."""
    mats = _pool(data.get('MaterialPool'))

    def mat_name(prim):
        try:
            return mats[int(prim.get('MaterialReference'))]['Name']
        except (TypeError, ValueError, IndexError, KeyError):
            return prim.get('MaterialName', 'material')

    def prim_index(key):
        try:
            return int(str(key).split('#')[-1])
        except ValueError:
            return -1

    # groups: (object name, material name, [(local influence names, prim, prim index)])
    groups_ = {}
    for node in mesh_nodes(data):
        infl = node.get('Influences') or {}
        local = [infl['Influence#%d' % i].get('Name') for i in range(len(infl))]
        for key, prim in node['Primitives'].items():
            if lods is not None and _lod(prim) is not None and _lod(prim) not in lods:
                continue
            mn = mat_name(prim)
            if SPLIT_BY_PRIMITIVE:
                lod = _lod(prim)
                gname = mn if lod is None else '%s_LOD%d' % (mn, lod)
                gkey = (node.get('NodeName'), key)
            else:
                gname, gkey = mn, mn
            groups_.setdefault(gkey, (gname, mn, []))[2].append((local, prim, prim_index(key)))
    objs = []
    for gname, mn, prims in groups_.values():
        P, UV, W, T = [], [], [], []
        TG, SG = [], []                 # v2.0.39: the file's tangents and bitangent signs (per vertex)
        for local, prim, _pi in prims:
            pos, uvs, skin, tris = _decode(vb, ib, data, prim)
            tg, sg = _decode_frame(vb, prim)
            if tg is None:
                tg, sg = [(0.0, 0.0, 0.0)] * len(pos), [1.0] * len(pos)
            TG += tg
            SG += sg
            if shift is not None and shift.length > 0:
                pos = [(x + shift.x, y + shift.y, z + shift.z) for x, y, z in pos]
            base = len(P)
            P += pos
            UV += uvs
            W += [[(local[b] if 0 <= b < len(local) else None, w) for b, w in s] for s in skin]
            T += [(a + base, b + base, c + base) for a, b, c in tris]
        me = bpy.data.meshes.new(gname)
        me.from_pydata(P, [], T)
        me.update()
        for poly in me.polygons:
            poly.use_smooth = True
        uvl = me.uv_layers.new(name='UVMap')
        for loop in me.loops:
            uvl.data[loop.index].uv = UV[loop.vertex_index]
        if any(t != (0.0, 0.0, 0.0) for t in TG):   # for the game-shading materials (c3exact); exports ignore them
            ta = me.attributes.new(TANGENT_ATTR, 'FLOAT_VECTOR', 'POINT')
            ta.data.foreach_set('vector', [c for t in TG for c in t])
            sa = me.attributes.new(TSIGN_ATTR, 'FLOAT', 'POINT')
            sa.data.foreach_set('value', SG)
        me.materials.append(bpy.data.materials.get(mn) or bpy.data.materials.new(mn))
        obj = bpy.data.objects.new(gname, me)
        if SPLIT_BY_PRIMITIVE and len(prims) == 1:
            obj['c3_primitive'] = prims[0][2]       # index in the .oct's Primitives
            lod = _lod(prims[0][1])
            if lod is not None:
                obj['c3_lod'] = lod
        bpy.context.collection.objects.link(obj)
        groups = {}
        for vi, pairs in enumerate(W):
            for bone, w in pairs:
                if bone is None:
                    continue
                g = groups.get(bone) or obj.vertex_groups.new(name=bone)
                groups[bone] = g
                g.add([vi], w, 'REPLACE')
        mod = obj.modifiers.new('Armature', 'ARMATURE')
        mod.object = arm
        obj.parent = arm
        objs.append(obj)
    return objs


# ------------------------------------------------------------------ props
# Any other Cars 3 .oct (weapons, pickups, level props ...): no skin, the
# parts are placed by the scene tree. Every Transform and Geometry node
# becomes a bone (same name, same hierarchy, rest = the node's place); the
# geometry becomes one mesh per material whose parts are each bound wholly to
# their own node's bone, so moving a node's bone moves that part and everything under it.
PROP_NODE = 'c3_node'                   # scene tree node index (bone property, face attribute)
# False: one object per material, named after it (a vertex group per node,
# each face's node in the c3_node face attribute). True: one object per
# primitive.
PROP_SPLIT_PARTS = False


def is_character(data):
    """True when the file has skinned geometry (a character)."""
    return any(n.get('Influences') for n in mesh_nodes(data))


def scene_nodes(data):
    """[(index, node, parent index or None, world matrix)] in file order,
    game space; the Scene root is left out."""
    nodes = _pool(data['SceneTreeNodePool'])
    world = {}

    def matrix(i, depth=0):
        if i not in world:
            n = nodes[i]
            refs = n.get('ParentNodeReferences') or []
            p = refs[0] if refs and isinstance(refs[0], int) and 0 <= refs[0] < len(nodes) and depth < 256 else None
            local = _mat_row_major(n['LocalToParentMatrix']) if n.get('LocalToParentMatrix') else mathutils.Matrix.Identity(4)
            world[i] = (matrix(p, depth + 1) @ local) if p is not None else local
        return world[i]
    out = []
    for i, n in enumerate(nodes):
        if n.get('Type') not in ('Transform', 'Geometry'):
            continue
        refs = n.get('ParentNodeReferences') or []
        p = refs[0] if refs and isinstance(refs[0], int) else None
        if p is not None and nodes[p].get('Type') not in ('Transform', 'Geometry'):
            p = None                        # under the Scene root
        out.append((i, n, p, matrix(i)))
    return out


def build_prop(name, data, vb, ib):
    """Armature of the scene tree + one mesh per primitive (see above)."""
    tree = scene_nodes(data)
    arm_data = bpy.data.armatures.new(name)
    arm = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    bone_of = {}                            # node index -> bone name (names repeat in files)
    for i, n, _p, m in tree:
        eb = arm_data.edit_bones.new(n.get('NodeName') or 'Node%d' % i)
        loc, rot, _s = m.decompose()
        eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, 0.05, 0.0)
        eb.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        bone_of[i] = eb.name
    for i, _n, p, _m in tree:
        if p in bone_of:
            arm_data.edit_bones[bone_of[i]].parent = arm_data.edit_bones[bone_of[p]]
    bpy.ops.object.mode_set(mode='OBJECT')
    for i, _n, _p, _m in tree:
        arm_data.bones[bone_of[i]][PROP_NODE] = i

    mats = _pool(data.get('MaterialPool'))
    # parts: (object name, [(node index, material, positions, uvs, triangles)])
    parts = {}
    for i, n, _p, m in tree:
        prims = list((n.get('Primitives') or {}).values()) if n.get('Type') == 'Geometry' else []
        flip = m.determinant() < 0          # mirrored node: keep the faces facing out
        for k, prim in enumerate(prims):
            try:
                mn = mats[int(prim.get('MaterialReference'))]['Name']
            except (TypeError, ValueError, IndexError, KeyError):
                mn = prim.get('MaterialName', 'material')
            pos, uvs, _skin, tris = _decode(vb, ib, data, prim, skinned=False)
            pos = [tuple(m @ mathutils.Vector(v)) for v in pos]
            if flip:
                tris = [(a, c, b) for a, b, c in tris]
            if PROP_SPLIT_PARTS:
                oname = bone_of[i] if len(prims) == 1 else '%s_%d' % (bone_of[i], k)
            else:
                oname = mn                  # one object per material, named after it
            parts.setdefault(oname, []).append((i, mn, pos, uvs, tris))

    meshes = []
    for oname, pieces in parts.items():
        P, UV, T, F_mat, F_node, slots = [], [], [], [], [], []
        groups = {}                         # bone -> vertex indices
        for i, mn, pos, uvs, tris in pieces:
            base = len(P)
            if mn not in slots:
                slots.append(mn)
            P += pos
            UV += uvs
            T += [(a + base, b + base, c + base) for a, b, c in tris]
            F_mat += [slots.index(mn)] * len(tris)
            F_node += [i] * len(tris)
            groups.setdefault(bone_of[i], []).extend(range(base, base + len(pos)))
        me = bpy.data.meshes.new(oname)
        me.from_pydata(P, [], T)
        me.update()
        for mn in slots:
            me.materials.append(bpy.data.materials.get(mn) or bpy.data.materials.new(mn))
        me.polygons.foreach_set('material_index', F_mat)
        me.polygons.foreach_set('use_smooth', [True] * len(T))
        me.attributes.new(PROP_NODE, 'INT', 'FACE').data.foreach_set('value', F_node)
        uvl = me.uv_layers.new(name='UVMap')
        for loop in me.loops:
            uvl.data[loop.index].uv = UV[loop.vertex_index]
        if any(t != (0.0, 0.0, 0.0) for t in TG):   # for the game-shading materials (c3exact); exports ignore them
            ta = me.attributes.new(TANGENT_ATTR, 'FLOAT_VECTOR', 'POINT')
            ta.data.foreach_set('vector', [c for t in TG for c in t])
            sa = me.attributes.new(TSIGN_ATTR, 'FLOAT', 'POINT')
            sa.data.foreach_set('value', SG)
        obj = bpy.data.objects.new(oname, me)
        bpy.context.collection.objects.link(obj)
        for bone, verts in groups.items():
            obj.vertex_groups.new(name=bone).add(verts, 1.0, 'REPLACE')
        obj.modifiers.new('Armature', 'ARMATURE').object = arm
        obj.parent = arm
        meshes.append(obj)
    return arm, len(tree), meshes


# ------------------------------------------------------------------ operator
# The skin bones a control moves (Cars 2 naming): X follows X_Ctrl, except
# these. The game connects them through its rig processors; in Blender each
# skin bone copies a hidden helper bone that sits exactly on it and hangs
# under its control - nothing moves at rest, the bone turns about the
# control's pivot when the control moves, and Copy Transforms replaces the
# bone's own parenting so no motion is applied twice.
FOLLOW_SPECIAL = {'Body': 'Body_Final_Proxy',
                  'TireFLeft_centerJoint': 'FLeft_Center_Ctrl', 'TireFRight_centerJoint': 'FRight_Center_Ctrl',
                  'TireBLeft_centerJoint': 'BLeft_Center_Ctrl', 'TireBRight_centerJoint': 'BRight_Center_Ctrl'}
FOLLOW_CONSTRAINT = 'Cars2 Follow'
FOLLOW_SUFFIX = '_Follow'


def follow_pairs(arm):
    """[(skin bone, control)] for the skin bones a control moves."""
    bones = arm.data.bones
    out = []
    for b in bones:
        if not b.use_deform:
            continue
        c = FOLLOW_SPECIAL.get(b.name)
        if c is None and _hub_side(b.name) and b.name == _hub_name(bones.keys(), _hub_side(b.name)):
            c = '%s_Center_Ctrl' % _hub_side(b.name)
        if c is None or c not in bones:
            c = b.name + '_Ctrl'
        if c in bones and not bones[c].use_deform:
            out.append((b.name, c))
    return out


SPIN_LINK = 'Cars2 Spin Link'


def spin_links(arm):
    """[(extra spin joint, the spin joint it copies)]: a spin joint hanging
    under another wheel's hub copies that hub's own spin joint - Mack's
    middle tires (TireMidLeft_SpinJoint_OL under TireBLeft_centerJoint) spin
    with his back tires (Cars 3: a DilationOrientationConnection)."""
    bones = arm.data.bones
    out = []
    for b in bones:
        if '_SpinJoint' not in b.name or b.parent is None or not _hub_side(b.parent.name):
            continue
        own = 'Tire%s_SpinJoint' % _hub_side(b.parent.name)
        if own != b.name and own in bones:
            out.append((b.name, own))
    return out


def add_spin_drivers(arm):
    """Wheel spin, as the Cars 2 import does it: clips spin the wheels through
    the Main_Ctrl/RotateFront and RotateRear values (degrees) - a converted
    Cars 3 clip too (its per-wheel Spin channels become those). Each wheel's
    own spin joint turns about its axle (X, as Cars 3's rig spins it) by that
    value; extra spin joints (Mack's middle tires) copy theirs (link_spins)."""
    n = 0
    bones = arm.data.bones
    for b in bones:
        if not b.name.endswith('_SpinJoint') or b.parent is None:
            continue
        if b.parent.name != _hub_name(bones.keys(), b.name[len('Tire'):-len('_SpinJoint')]):
            continue
        prop = 'Main_Ctrl/RotateFront' if b.name.startswith('TireF') else 'Main_Ctrl/RotateRear'
        drivers = arm.animation_data.drivers if arm.animation_data else []
        if any(d.data_path == 'pose.bones["%s"].rotation_euler' % b.name for d in drivers):
            continue
        c2_model._add_spin_driver(arm, b.name, 0, prop, math.pi / 180.0, 0.0)
        n += 1
    return n


def link_spins(arm):
    n = 0
    for b, src in spin_links(arm):
        pb = arm.pose.bones[b]
        if any(k.name == SPIN_LINK for k in pb.constraints):
            continue
        k = pb.constraints.new('COPY_ROTATION')
        k.name = SPIN_LINK
        k.target, k.subtarget = arm, src
        k.owner_space = k.target_space = 'LOCAL'
        n += 1
    return n


# v2.0.51: Mater's tow cable rig as the game builds it. The cable ends and the hook ride on proxies under
# TowCrane_Ctrl / on the cable end; the cable bases at the back aim at their ends (Cars 3 aimConstraint1
# nodes). The skin binds them under Main otherwise, so a swung crane left cables and hook behind.
CABLE_FOLLOW = {'CableRigEndCenter': 'CableRigEndCenter_Proxy',
                'CableRigWheelEndLeft': 'CableRigWheelEndLeft_Proxy',
                'CableRigWheelEndRight': 'CableRigWheelEndRight_Proxy',
                'TowHook': 'TCable_End'}
CABLE_AIM = {'CableRigBaseCenter': 'CableRigEndCenter', 'CableRigBaseLeft': 'CableRigEndLeft',
             'CableRigBaseRight': 'CableRigEndRight', 'CableRigBaseWheelCenter': 'CableRigEndCenter',
             'CableRigBaseWheelLeft': 'CableRigWheelEndLeft', 'CableRigBaseWheelRight': 'CableRigWheelEndRight'}
CABLE_ANCHOR = 'BackEnd'
AIM_SUFFIX = '_Aim'


AXLE_AIM = 'Cars3 Axle Aim'


def axle_links(context, arm):
    """v2.0.54: axles (Arvy's axelLeft / axelRight) as the game rigs them: each sits on its wheel hub and
    aims at the opposite hub (Cars 3 B<side>_AxelAim_Proxy aim constraints), so the bar tilts with the
    suspension. Returns the number of axles linked."""
    import re
    bones = arm.data.bones
    todo = []
    for b in bones:
        m = re.match(r'^ax(?:el|le)(Left|Right)$', b.name, re.I)
        if not m or any(k.name == FOLLOW_CONSTRAINT for k in arm.pose.bones[b.name].constraints):
            continue
        hubs = [h for h in (_hub_name(bones.keys(), s) for s in ('FLeft', 'FRight', 'BLeft', 'BRight')) if h]
        if not hubs:
            continue
        hub = min(hubs, key=lambda h: (bones[h].head_local - b.head_local).length)
        other = hub.replace('Left', '#').replace('Right', 'Left').replace('#', 'Right')
        if other in bones and other != hub:
            todo.append((b.name, hub, other))
    if not todo:
        return 0
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    internals = arm.data.collections.get('Rig Internals') if hasattr(arm.data, 'collections') else None
    for name, hub, other in todo:
        src = eb[name]
        d = eb[other].head - src.head
        ln = max((src.tail - src.head).length, 0.01)
        for hname, head, tail, roll, parent in ((name + AIM_SUFFIX, src.head.copy(),
                                                 src.head + (d.normalized() * ln if d.length > 1e-6 else src.tail - src.head),
                                                 0.0, hub),
                                                (name + FOLLOW_SUFFIX, src.head.copy(), src.tail.copy(), src.roll,
                                                 name + AIM_SUFFIX)):
            h = eb.get(hname) or eb.new(hname)
            h.head, h.tail, h.roll = head, tail, roll
            h.parent = eb[parent]
            h.use_deform = False
            if internals is not None:
                internals.assign(h)
    bpy.ops.object.mode_set(mode='OBJECT')
    for name, hub, other in todo:
        k = arm.pose.bones[name + AIM_SUFFIX].constraints.new('DAMPED_TRACK')
        k.name = AXLE_AIM
        k.target, k.subtarget, k.track_axis = arm, other, 'TRACK_Y'
        k = arm.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        k.name = FOLLOW_CONSTRAINT
        k.target, k.subtarget = arm, name + FOLLOW_SUFFIX
    context.view_layer.objects.active = prev
    return len(todo)


def cable_links(context, arm):
    """Hook up the tow cable rig; returns the number of bones linked."""
    bones = arm.data.bones
    follow = [(s, c) for s, c in CABLE_FOLLOW.items() if s in bones and c in bones]
    aim = [(s, t) for s, t in CABLE_AIM.items() if s in bones and t in bones and CABLE_ANCHOR in bones]
    todo = [s for s, _ in follow + aim
            if not any(k.name == FOLLOW_CONSTRAINT for k in arm.pose.bones[s].constraints)]
    if not todo:
        return 0
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    internals = arm.data.collections.get('Rig Internals') if hasattr(arm.data, 'collections') else None

    def helper(name, head, tail, roll, parent):
        h = eb.get(name) or eb.new(name)
        h.head, h.tail, h.roll = head, tail, roll
        h.parent = eb[parent]
        h.use_deform = False
        if internals is not None:
            internals.assign(h)
        return h
    for s, c in follow:
        if s in todo:
            src = eb[s]
            helper(s + FOLLOW_SUFFIX, src.head.copy(), src.tail.copy(), src.roll, c)
    for s, t in aim:
        if s in todo:
            src = eb[s]
            ln = max((src.tail - src.head).length, 0.01)
            d = eb[t].head - src.head
            tail = src.head + (d.normalized() * ln if d.length > 1e-5 else (src.tail - src.head))
            a = helper(s + AIM_SUFFIX, src.head.copy(), tail, 0.0, CABLE_ANCHOR)
            helper(s + FOLLOW_SUFFIX, src.head.copy(), src.tail.copy(), src.roll, a.name)
    bpy.ops.object.mode_set(mode='OBJECT')
    for s, t in aim:
        if s in todo:
            k = arm.pose.bones[s + AIM_SUFFIX].constraints.new('DAMPED_TRACK')
            k.name = 'Cars3 Aim'
            k.target, k.subtarget, k.track_axis = arm, t, 'TRACK_Y'
    for s in todo:
        k = arm.pose.bones[s].constraints.new('COPY_TRANSFORMS')
        k.name = FOLLOW_CONSTRAINT
        k.target, k.subtarget = arm, s + FOLLOW_SUFFIX
    context.view_layer.objects.active = prev
    return len(todo)


def connect_controls(context, arm):
    """Make the skin bones follow their controls, so clips (which move the
    controls) move the model; extra spin joints spin with their wheel.
    Returns the number of bones connected."""
    linked = add_spin_drivers(arm) + link_spins(arm)
    pairs = [(s, c) for s, c in follow_pairs(arm)
             if not any(k.name == FOLLOW_CONSTRAINT for k in arm.pose.bones[s].constraints)]
    if not pairs:
        return linked + cable_links(context, arm) + axle_links(context, arm)
    prev = context.view_layer.objects.active
    prev_mode = arm.mode if context.view_layer.objects.active == arm else 'OBJECT'
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    internals = arm.data.collections.get('Rig Internals') if hasattr(arm.data, 'collections') else None
    for s, c in pairs:
        h = eb.get(s + FOLLOW_SUFFIX) or eb.new(s + FOLLOW_SUFFIX)
        src = eb[s]
        h.head, h.tail, h.roll = src.head.copy(), src.tail.copy(), src.roll
        h.parent = eb[c]
        h.use_deform = False
        if internals is not None:
            internals.assign(h)
    bpy.ops.object.mode_set(mode='OBJECT')
    for s, c in pairs:
        k = arm.pose.bones[s].constraints.new('COPY_TRANSFORMS')
        k.name = FOLLOW_CONSTRAINT
        k.target = arm
        k.subtarget = s + FOLLOW_SUFFIX
    if prev_mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode=prev_mode)
        except RuntimeError:
            pass
    context.view_layer.objects.active = prev
    return len(pairs) + linked + cable_links(context, arm) + axle_links(context, arm)


def shape_chains(chains, binds):
    """The two chain setups that worked in-game, chosen by the chain's shape:
    - hanging (its bones run mostly downwards - Fritter's right chain,
      Proxy01 2.40 high, Proxy02 1.82): a plain chain over its own bones,
      root first, like Mater's tow cable;
    - lying sideways (her left chain, 2.7 along her side): the anchor ->
      the swinging proxy, the other bones turning with it.
    (A separate particle bone or a mount above the chain made both worse.)"""
    import c3toc2
    out = []
    for c in chains:
        riders = list(c.get('riders') or [])
        c = dict(c, particle=None, riders=[])
        proxies = [b for b in c.get('bones', []) if b != c.get('anchor')] + riders + list(c.get('followers') or [])
        proxies = [b for b in c3toc2.order_chain(set(proxies)) if b in binds]
        if len(proxies) >= 2:
            a = binds[proxies[0]].translation
            z = binds[proxies[-1]].translation
            down = a[1] - z[1]                      # game space: Y up
            side = math.hypot(z[0] - a[0], z[2] - a[2])
            if down > side:                         # hanging
                c.update(bones=proxies, anchor=None, followers=[], fixed=[], shape='hanging')
                out.append(c)
                continue
        moving = riders or [b for b in c.get('bones', []) if b != c.get('anchor') and not b.endswith('_Particle')]
        if c.get('anchor') and not moving:
            moving = proxies[:1]
        c.update(bones=([c['anchor']] if c.get('anchor') else []) + moving, shape='lying')
        out.append(c)
    return out


def add_chain_anchors(context, arm, rig, chains):
    """A chain's fixed root is its anchor frame (chainLeft_Start); make it a
    weightless skin bone at that frame, under the nearest skin bone above it
    (Body), so the chain can hang from it."""
    if not rig:
        return
    names, parent = rig['names'], rig['parent']
    todo = [c['anchor'] for c in chains if c.get('anchor') and c['anchor'] not in arm.data.bones
            and c['anchor'] in names]
    if not todo and not any(c.get('particle') for c in chains):
        return
    prev = context.view_layer.objects.active
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    skin = arm.data.collections.get('Skin') if hasattr(arm.data, 'collections') else None
    for nm in todo:
        i = names.index(nm)
        loc, rot, _s = rig['world'](i).decompose()
        b = eb.new(nm)
        b.head, b.tail = (0.0, 0.0, 0.0), (0.0, 0.05, 0.0)
        b.matrix = mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        p = parent[i]
        while p is not None and names[p] not in eb:
            p = parent[p]
        b.parent = eb[names[p]] if p is not None else eb.get('Body')
        if skin is not None:
            skin.assign(b)
    # the swinging particle: hanging its offset from the anchor (in the
    # anchor's frame - Fritter: 0.7 down), with the moving proxies riding on it
    for c in chains:
        pt = c.get('particle')
        if not pt or c.get('anchor') not in eb:
            continue
        a = eb[c['anchor']]
        b = eb.get(pt['name']) or eb.new(pt['name'])
        b.head, b.tail = (0.0, 0.0, 0.0), (0.0, 0.05, 0.0)
        b.matrix = a.matrix @ mathutils.Matrix.Translation(mathutils.Vector(pt['offset']))
        b.parent = a
        if skin is not None:
            skin.assign(b)
        for r in c.get('riders') or ():
            if r in eb:
                eb[r].parent = b                       # rides on the particle, rest unchanged
    bpy.ops.object.mode_set(mode='OBJECT')
    context.view_layer.objects.active = prev


TEXTURE_NOTE = ''                        # last texture message, for the operator report


def skeleton_and_rig(data, vb, ib):
    """(skin binds, rig, ground shift) exactly as the import places them."""
    nodes = mesh_nodes(data)
    if not nodes:
        raise RuntimeError('No Cars 3 geometry (Geometry nodes with Primitives) in this file')
    binds = skin_binds(nodes)
    rig = read_rig(data, binds)              # (offset measured before any shift)
    shift = ground_shift(data, vb, ib)
    if shift.length > 0:
        binds = {k: mathutils.Matrix.Translation(shift) @ m for k, m in binds.items()}
        rig = read_rig(data, skin_binds(nodes), shift)
    return binds, rig, shift


def full_rig_pose(filepath):
    """{bone: game-space Matrix} of every bone a full import (control rig +
    FX markers) of this Cars 3 file has - built in a throw-away armature.
    For exports from an import without the control rig (v2.0.16)."""
    data, vb, ib, name = read_cars3(filepath)
    binds, rig, _shift = skeleton_and_rig(data, vb, ib)
    prev = bpy.context.view_layer.objects.active
    arm, _s, _n = build_armature(name + '_c3pose_tmp', binds, rig, True, True)
    try:
        return {b.name: b.matrix_local.copy() for b in arm.data.bones}
    finally:
        ad = arm.data
        bpy.data.objects.remove(arm)
        bpy.data.armatures.remove(ad)
        bpy.context.view_layer.objects.active = prev


def import_cars3(context, filepath, read=None, textures=True, control_rig=True, markers=True, lods=None):
    global TEXTURE_NOTE
    data, vb, ib, name = read or read_cars3(filepath)
    binds, rig, shift = skeleton_and_rig(data, vb, ib)
    arm, skin, nrig = build_armature(name, binds, rig, control_rig, markers)
    meshes = build_meshes(data, vb, ib, arm, shift, lods)
    if not meshes:
        raise RuntimeError('none of the picked LODs (%s) is in this file' % ', '.join(str(x) for x in sorted(lods or ())))
    arm.rotation_euler = (math.radians(c2_model.GLOBAL_ROTATE_X_DEGREES), 0.0, 0.0)
    arm[PROP_SOURCE] = os.path.abspath(filepath)
    TEXTURE_NOTE = ''
    if textures:
        try:
            from . import cars3_textures
            nmat, nimg, note = cars3_textures.apply(context, filepath, name, meshes)
            TEXTURE_NOTE = ('%d materials textured, %d images' % (nmat, nimg)) + ('; ' + note if note else '')
        except Exception as e:
            import traceback
            traceback.print_exc()
            TEXTURE_NOTE = 'textures failed: %s' % e
    try:                                   # dynamic chains (Fritter's side chains ...)
        import c3toc2
        chains = shape_chains(c3toc2.find_cars3_chains(data, set(binds)), binds)
        if chains:
            add_chain_anchors(context, arm, rig, chains)
            arm[PROP_CHAINS] = json.dumps(chains)
    except Exception as e:
        print('Cars 3 import: chains not read: %s' % e)
    if control_rig:
        try:                               # v2.0.53: the chains swing in Blender as in the game
            from . import c3chains
            c3chains.setup(context, arm, data, rig)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('Cars 3 import: chain simulation not set up: %s' % e)
    try:                                   # clips move controls; let them move the model
        if control_rig:
            connect_controls(context, arm)
    except Exception as e:
        print('Cars 3 import: controls not connected to the skin: %s' % e)
    if textures:                           # v2.0.36: eyes drawn like the game's eye render target
        try:
            from . import c3eyes
            note = c3eyes.setup(arm, meshes, data)
            if note:
                TEXTURE_NOTE = (TEXTURE_NOTE + '; ' if TEXTURE_NOTE else '') + note
        except Exception as e:
            import traceback
            traceback.print_exc()
            TEXTURE_NOTE = (TEXTURE_NOTE + '; ' if TEXTURE_NOTE else '') + 'eyes failed: %s' % e
    if getattr(c2_model, 'MODEL_COLLECTION', True):
        col = bpy.data.collections.new(name)
        context.scene.collection.children.link(col)
        for o in [arm] + meshes:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            col.objects.link(o)
    return arm, len(skin), nrig, len(meshes)


def import_prop(context, filepath, read=None):
    data, vb, ib, name = read or read_cars3(filepath)
    arm, nbones, meshes = build_prop(name, data, vb, ib)
    if not meshes:
        raise RuntimeError('No geometry (Geometry nodes with Primitives) in this file')
    arm.rotation_euler = (math.radians(c2_model.GLOBAL_ROTATE_X_DEGREES), 0.0, 0.0)
    arm[PROP_PROP_SOURCE] = os.path.abspath(filepath)
    if getattr(c2_model, 'MODEL_COLLECTION', True):
        col = bpy.data.collections.new(name)
        context.scene.collection.children.link(col)
        for o in [arm] + meshes:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            col.objects.link(o)
    return arm, nbones, len(meshes)


def import_oct(context, filepath, textures=True, control_rig=True, markers=True, lods=None):
    """Any Cars 3 .oct: a character (skinned) gets the Cars 2 layout, anything
    else (weapons, props) its own scene tree. -> report text."""
    read = read_cars3(filepath)
    if is_character(read[0]):
        arm, nskin, nrig, nmesh = import_cars3(context, filepath, read, textures, control_rig, markers, lods)
        return 'Imported character %s: %d skin bones, %d %s, %d meshes (LOD %s)%s' \
            % (arm.name, nskin, nrig, 'rig bones (Cars 2 layout)' if control_rig else 'FX markers', nmesh,
               ', '.join(str(x) for x in sorted(lods)) if lods is not None else 'all',
               (' - ' + TEXTURE_NOTE) if TEXTURE_NOTE else '')
    arm, nbones, nmesh = import_prop(context, filepath, read)
    return 'Imported %s: %d node bones, %d meshes' % (arm.name, nbones, nmesh)


class CARS2TK_OT_import_cars3(bpy.types.Operator, ImportHelper):
    """Import a Cars 3 .oct: characters get the Cars 2 armature layout and their textures; weapons and other props their node tree"""
    bl_idname = 'cars2tk.import_cars3'
    bl_label = 'Import Cars 3 Character'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.oct'
    filter_glob: StringProperty(default='*.oct;*.json', options={'HIDDEN'})
    # v2.0.16: the Cars 2 import's options + the LOD sets (Cars 3 models carry three)
    control_rig: BoolProperty(name='Import Control Rig', default=True,
                              description="Imports the character's control rig (controls, wheel spin, links to the "
                                          'skin), for animation purposes. Off: the skin bones only')
    textures: BoolProperty(name='Extract Textures', default=True,
                           description='Converts the PS4 .tbody textures (the textures/ folder of the extracted '
                                       "character bundle, else the game's bundle zip) to DDS with ditex and loads them")
    markers: BoolProperty(name='Import FX Markers', default=True,
                          description="Imports the character's FX markers, for weapons, exhausts etc.")
    # v2.0.18: a 'LOD Models' fold-out section with one checkbox per set
    lod0: BoolProperty(name='LOD 0', default=True, description='Full detail - the set the game shows up close')
    lod1: BoolProperty(name='LOD 1', default=False, description='Medium detail')
    lod2: BoolProperty(name='LOD 2', default=False, description='Lowest detail')
    show_lods: BoolProperty(name='LOD Models', default=True, options={'SKIP_SAVE'},
                            description='Show or hide the LOD model choice')

    def picked_lods(self):
        return {i for i, on in enumerate((self.lod0, self.lod1, self.lod2)) if on}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        layout.use_property_decorate = False
        col = layout.column()                       # same spacing as the Cars 2 import dialog
        col.prop(self, 'control_rig')
        col.prop(self, 'textures')
        col.prop(self, 'markers')
        # v2.0.35: 'LOD Models' opens/closes with a plain left-aligned arrow (no panel header bar)
        layout.separator()
        row = layout.row()
        row.alignment = 'LEFT'
        row.prop(self, 'show_lods', emboss=False,
                 icon='DOWNARROW_HLT' if self.show_lods else 'RIGHTARROW')
        if self.show_lods:
            col = layout.column()
            for p in ('lod0', 'lod1', 'lod2'):
                col.prop(self, p)

    def execute(self, context):
        lods = self.picked_lods()
        if not lods:
            self.report({'ERROR'}, 'Tick at least one LOD model set')
            return {'CANCELLED'}
        try:
            msg = import_oct(context, self.filepath, self.textures, self.control_rig, self.markers, lods)
        except Exception as e:
            self.report({'ERROR'}, 'Cars 3 import failed: %s' % e)
            return {'CANCELLED'}
        self.report({'WARNING'} if 'textures failed' in msg or 'no textures' in msg or 'texture warnings' in msg
                    else {'INFO'}, msg)
        return {'FINISHED'}


classes = (CARS2TK_OT_import_cars3,)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
