# Four-router cEOS lab

```text
                     R2
                    /  \
Host A ---------- R1    R4 ---------- Host B
                    \  /
                     R3
```

All transit interfaces run OSPF process 1 in area 0. Host-facing interfaces
are passive OSPF interfaces. The path through R2 has cost 20 between R1 and R4;
the path through R3 has cost 200. Each host-facing interface has the same default
cost, so R2 is preferred in both directions.

| Connection | First endpoint | Second endpoint |
| --- | --- | --- |
| R1 Ethernet1 — R2 Ethernet1 | 10.0.12.1/30 | 10.0.12.2/30 |
| R1 Ethernet2 — R3 Ethernet1 | 10.0.13.1/30 | 10.0.13.2/30 |
| R2 Ethernet2 — R4 Ethernet1 | 10.0.24.1/30 | 10.0.24.2/30 |
| R3 Ethernet2 — R4 Ethernet2 | 10.0.34.1/30 | 10.0.34.2/30 |
| R1 Ethernet3 — Host A eth1 | 10.10.1.1/24 | 10.10.1.10/24 |
| R4 Ethernet3 — Host B eth1 | 10.10.2.1/24 | 10.10.2.10/24 |

Management uses the existing `advnet-ceos-mgmt` Docker network. Router management
addresses are 172.30.100.11–14 in a separate MGMT VRF. Host management addresses
are 172.30.100.101–102. Hosts have explicit routes to the opposite data subnet
and no default route. Test pings bind to eth1. Test traffic must traverse the
routers rather than Docker's management bridge.

From the project root:

```bash
sudo containerlab deploy -t labs/diamond.clab.yml
sudo bash scripts/check-diamond.sh
```

The initial deployment pulls `alpine:3.22.1` for the two hosts. Routers use the
locally imported `ceos:4.36.3M`. The checker waits for connectivity, shows OSPF
neighbors and host routes, pings in both directions, and prints a traceroute.
Each router should have two FULL neighbors. The preferred path should traverse
R1, R2, and R4; traceroute reply addresses can vary by interface.

Baseline source configurations live in `labs/configs/diamond/r1.cfg` through
`r4.cfg`. Generated saved configurations live in
`labs/clab-advnet-diamond/<router>/flash/startup-config`.
Existing generated configs take precedence on subsequent deployments. To reset
this lab to the baseline after experimenting (discarding its runtime changes):

```bash
sudo containerlab destroy -t labs/diamond.clab.yml --cleanup
sudo containerlab deploy -t labs/diamond.clab.yml
```

The separate `ceos-smoke` lab can remain running. To stop it and free resources:

```bash
sudo containerlab destroy -t labs/ceos-smoke.clab.yml
```

Live forwarding and EOS configuration acceptance must be checked after deployment.
Batfish validation is a subsequent milestone and has not yet been performed.
