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
    n_gws_per_project: int = 1
    n_vms_per_project: int = 3


class BaseOpenstack(ExtCmd):
    def __init__(self, config, cluster, global_cfg):
        super().__init__(config, cluster)
        test_config = config.get("base_openstack")
        self.config = BaseOpenstackConfig(**test_config)

    def run(self, ovn: OpenStackCloud, global_cfg):
        # create ovn topology
        worker_count = len(ovn.worker_nodes)
        with Context(ovn, "base_openstack_bringup", worker_count) as ctx:
            for i in ctx:
                worker_node: ChassisNode = ovn.worker_nodes[i]
                log.info(
                    f"Provisioning {worker_node.__class__.__name__} "
                    f"({i+1}/{worker_count})"
                )
                worker_node.provision(ovn)

        with Context(ovn, "base_openstack_provision") as ctx:
            for _ in range(self.config.n_projects):
                _ = ovn.new_project(gw_nodes=self.config.n_gws_per_project)

            for project in ovn.projects:
                for index in range(self.config.n_vms_per_project):
                    ovn.add_vm_to_project(
                        project, f"{project.uuid[:6]}-{index}"
                    )

        with Context(ovn, "base_openstack"):
            for project in ovn.projects:
                ovn.mesh_ping_ports(project.vm_ports)
