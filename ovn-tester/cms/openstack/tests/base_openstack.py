import logging

from dataclasses import dataclass

from ovn_ext_cmd import ExtCmd
from ovn_context import Context
from ovn_workload import ChassisNode

from cms.openstack import OpenStackCloud

log = logging.getLogger(__name__)


@dataclass
class BaseOpenstackConfig:

    n_projects: int = 1


class BaseOpenstack(ExtCmd):
    def __init__(self, config, cluster, global_cfg):
        super().__init__(config, cluster)
        test_config = config.get("base_openstack")
        self.config = BaseOpenstackConfig(**test_config)

    def run(self, ovn: OpenStackCloud, global_cfg):
        # create ovn topology
        worker_count = len(ovn.worker_nodes)
        with Context(ovn, "base_cluster_bringup", worker_count) as ctx:
            for i in ctx:
                worker_node: ChassisNode = ovn.worker_nodes[i]
                log.info(
                    f"Provisioning {worker_node.__class__.__name__} "
                    f"({i+1}/{worker_count})"
                )
                worker_node.provision(ovn)

            for _ in range(self.config.n_projects):
                _ = ovn.new_project(gw_nodes=ovn.worker_nodes)
