"""Pure HITL v2 configuration contracts.

This package deliberately contains no ROS dependencies or runtime adapters.
"""

from oven_runtime.v2.profile import HitlV2Profile, load_hitl_v2_profile

__all__ = ("HitlV2Profile", "load_hitl_v2_profile")
