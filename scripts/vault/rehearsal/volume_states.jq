# The state of the two recorded Vault volumes, for gke_rehearsal.sh (retain, cleanup, inspect).
# Input {recorded, pvs, pvcs, ns}: volumes.json, every PV in the cluster, the PVCs of ns.
# Output {volumes: [{claim, name, disk, reclaim, state}], problems: [text]}, with state one of:
#   bound      the PVC is Bound to the recorded PV, which claims exactly that PVC (same uid)
#   binding    the PVC was recreated with volumeName and is not Bound yet; the PV is unclaimed or claims it
#   unclaimed  no PVC; the PV is Available with its claimRef cleared (a recovery stopped before the PVC)
#   released   no PVC; the PV is Released and still names the recorded claim
#   gone       neither PVC nor PV (already deleted; the disk must still be proven absent)
# Anything else is a problem, and cleanup refuses before its first deletion.
. as $in
| def find($items; $n): first($items[] | select(.metadata.name == $n)) // null;
  def state($r; $pv; $pvc):
    ($pv.spec.claimRef // null) as $ref
    | if $pv != null and ($pv.spec.csi.volumeHandle // null) != $r.disk then
        {problem: "PV \($r.name): disk \($pv.spec.csi.volumeHandle // "none") differs from the recorded \($r.disk // "none")"}
      elif $pv != null and ($pv.spec.persistentVolumeReclaimPolicy | IN("Delete", "Retain") | not) then
        {problem: "PV \($r.name): unexpected reclaim policy \($pv.spec.persistentVolumeReclaimPolicy)"}
      elif $ref != null and ($ref.namespace != $in.ns or $ref.name != $r.claim) then
        {problem: "PV \($r.name): claimed by \($ref.namespace)/\($ref.name), not the recorded \($in.ns)/\($r.claim)"}
      elif $pvc != null and $pvc.spec.volumeName != $r.name then
        {problem: "PVC \($r.claim): bound to \($pvc.spec.volumeName // "no volume"), not the recorded \($r.name)"}
      elif $pvc != null and $pv == null then {problem: "PVC \($r.claim): its PV \($r.name) is missing"}
      elif $pvc != null and $pvc.status.phase == "Bound" and $ref.uid == $pvc.metadata.uid then {state: "bound"}
      elif $pvc != null and ($ref == null or ($ref.uid // null) == null or $ref.uid == $pvc.metadata.uid) then {state: "binding"}
      elif $pvc != null then {problem: "PVC \($r.claim): PV \($r.name) still names an older claim, so it cannot bind"}
      elif $pv == null then {state: "gone"}
      elif $ref == null and $pv.status.phase == "Available" then {state: "unclaimed"}
      elif $ref != null and $pv.status.phase == "Released" then {state: "released"}
      else {problem: "PV \($r.name): \($pv.status.phase) without its PVC"} end;
  [ $in.recorded[] as $r | find($in.pvs; $r.name) as $pv | find($in.pvcs; $r.claim) as $pvc
    | {claim: $r.claim, name: $r.name, disk: $r.disk, reclaim: ($pv.spec.persistentVolumeReclaimPolicy // null)} + state($r; $pv; $pvc) ]
| {volumes: [.[] | select(.state)],
   problems: ([.[] | .problem // empty]
     + [$in.pvs[] | select(.spec.claimRef.namespace == $in.ns and (.metadata.name | IN($in.recorded[].name) | not))
        | "unrecorded PV \(.metadata.name) claims \($in.ns)/\(.spec.claimRef.name)"]
     + [$in.pvcs[] | select(.metadata.name | IN($in.recorded[].claim) | not) | "unrecorded PVC \($in.ns)/\(.metadata.name)"])}
