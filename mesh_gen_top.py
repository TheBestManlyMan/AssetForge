# Mesh_Gen — Python Processor TOP
# Paste into the node's "Script Body" (Generate Work Items section).
#
# Spare parms to add on the node:
#   overwrite      Toggle   default 0   — re-run even if mesh.glb exists
#   topology       Menu     quad / triangle   default quad
#   target_polycount  Int   default 30000
#   enable_pbr     Toggle   default 1
#
# Input:  work items with  asset_dir, name, id  attribs (from Image_Gen)
# Output: same attribs forwarded + mesh_glb pointing at mesh.glb

import hou
import os
import sys

# Make meshy_client importable from $HIP without PYTHONPATH setup
hip = hou.text.expandString("$HIP")
if hip not in sys.path:
    sys.path.insert(0, hip)

# Force-reload so edits to meshy_client.py take effect without restarting Houdini
sys.modules.pop("meshy_client", None)
import meshy_client

# self.path is the HOU node path — hou.pwd() is not set in PDG callbacks
node = hou.node(self.path)
overwrite       = int(node.parm("overwrite").eval())
topology        = node.parm("topology").evalAsString()       # "quad" or "triangle"
target_polycount = int(node.parm("target_polycount").eval())
enable_pbr      = bool(int(node.parm("enable_pbr").eval()))

for upstream_item in upstream_items:
    try:
        asset_dir  = upstream_item.stringAttribValue("asset_dir")
        asset_name = upstream_item.stringAttribValue("name")
        asset_id   = upstream_item.stringAttribValue("id")

        in_img  = os.path.join(asset_dir, "generated.png")
        out_glb = os.path.join(asset_dir, "mesh.glb")

        if os.path.isfile(out_glb) and not overwrite:
            print("[mesh_gen] {}: cached → {}".format(asset_name, out_glb))
        else:
            if not os.path.isfile(in_img):
                raise FileNotFoundError(
                    "generated.png not found: {}".format(in_img)
                )
            print("[mesh_gen] {}: submitting to Meshy …".format(asset_name))
            meshy_client.generate(
                in_img, out_glb,
                topology=topology,
                target_polycount=target_polycount,
                enable_pbr=enable_pbr,
            )

        out_item = item_holder.addWorkItem(parent=upstream_item)
        # Forward attribs that downstream nodes need
        out_item.setStringAttrib("id",        asset_id)
        out_item.setStringAttrib("name",      asset_name)
        out_item.setStringAttrib("asset_dir", asset_dir)
        out_item.setStringAttrib("mesh_glb",  out_glb)
        out_item.addResultData(out_glb, "file/geo", 0)

    except Exception as e:
        raise RuntimeError(
            "[mesh_gen] work item '{}' failed: {}".format(upstream_item.name, e)
        ) from e
