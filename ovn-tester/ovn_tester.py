#!/usr/bin/env python3

import logging
import sys
import netaddr
import yaml
import importlib
import ovn_exceptions
import gc
import time

from itertools import chain
from collections import namedtuple
from ovn_context import Context
from ovn_sandbox import PhysicalNode
from ovn_workload import BrExConfig, ClusterConfig
from ovn_workload import CentralNode, WorkerNode, Cluster
from ovn_utils import DualStackSubnet, NodeConf
from ovs.stream import Stream


GlobalCfg = namedtuple(
    'GlobalCfg', ['log_cmds', 'cleanup', 'run_ipv4', 'run_ipv6']
)

ClusterBringupCfg = namedtuple('ClusterBringupCfg', ['n_pods_per_node'])


def usage(name):
    print(
        f'''
{name} PHYSICAL_DEPLOYMENT TEST_CONF
where PHYSICAL_DEPLOYMENT is the YAML file defining the deployment.
where TEST_CONF is the YAML file defining the test parameters.
''',
        file=sys.stderr,
    )


def read_physical_deployment(deployment, global_cfg):
    with open(deployment, 'r') as yaml_file:
        dep = yaml.safe_load(yaml_file)

        central_dep = dep['central-node']
        central_node = PhysicalNode(
            central_dep.get('name', 'localhost'), global_cfg.log_cmds
        )
        worker_nodes = [
            PhysicalNode(worker, global_cfg.log_cmds)
            for worker in dep['worker-nodes']
        ]
        return central_node, worker_nodes


# SSL files are installed by ovn-fake-multinode in these locations.
SSL_KEY_FILE = "/opt/ovn/ovn-privkey.pem"
SSL_CERT_FILE = "/opt/ovn/ovn-cert.pem"
SSL_CACERT_FILE = "/opt/ovn/pki/switchca/cacert.pem"


def read_config(config):
    global_args = config.get('global', dict())
    global_cfg = GlobalCfg(**global_args)

    cluster_args = config.get('cluster')
    cluster_cfg = ClusterConfig(
        monitor_all=cluster_args['monitor_all'],
        logical_dp_groups=cluster_args['logical_dp_groups'],
        clustered_db=cluster_args['clustered_db'],
        log_txns_db=cluster_args['log_txns_db'],
        datapath_type=cluster_args['datapath_type'],
        raft_election_to=cluster_args['raft_election_to'],
        node_net=netaddr.IPNetwork(cluster_args['node_net']),
        n_relays=cluster_args['n_relays'],
        n_az=cluster_args['n_az'],
        enable_ssl=cluster_args['enable_ssl'],
        northd_probe_interval=cluster_args['northd_probe_interval'],
        db_inactivity_probe=cluster_args['db_inactivity_probe'],
        node_timeout_s=cluster_args['node_timeout_s'],
        internal_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['internal_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['internal_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        external_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['external_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['external_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        gw_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['gw_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['gw_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        cluster_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['cluster_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['cluster_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        ts_net=DualStackSubnet(
            netaddr.IPNetwork(cluster_args['ts_net'])
            if global_cfg.run_ipv4
            else None,
            netaddr.IPNetwork(cluster_args['ts_net6'])
            if global_cfg.run_ipv6
            else None,
        ),
        n_workers=cluster_args['n_workers'],
        vips=cluster_args['vips'],
        vips6=cluster_args['vips6'],
        vip_subnet=cluster_args['vip_subnet'],
        static_vips=cluster_args['static_vips'],
        static_vips6=cluster_args['static_vips6'],
        use_ovsdb_etcd=cluster_args['use_ovsdb_etcd'],
        northd_threads=cluster_args['northd_threads'],
        ssl_private_key=SSL_KEY_FILE,
        ssl_cert=SSL_CERT_FILE,
        ssl_cacert=SSL_CACERT_FILE,
    )

    brex_cfg = BrExConfig(
        physical_net=cluster_args.get('physical_net', 'providernet'),
    )

    bringup_args = config.get('base_cluster_bringup', dict())
    bringup_cfg = ClusterBringupCfg(
        n_pods_per_node=bringup_args.get('n_pods_per_node', 10)
    )
    return global_cfg, cluster_cfg, brex_cfg, bringup_cfg


def setup_logging(global_cfg):
    FORMAT = '%(asctime)s | %(name)-12s |%(levelname)s| %(message)s'
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=FORMAT)
    logging.Formatter.converter = time.gmtime

    if gc.isenabled():
        # If the garbage collector is enabled, it runs from time to time, and
        # interrupts ovn-tester to do so. If we are timing an operation, then
        # the gc can distort the amount of time something actually takes to
        # complete, resulting in graphs with spikes.
        #
        # Disabling the garbage collector runs the theoretical risk of leaking
        # a lot of memory, but in practical tests, this has not been a
        # problem. If gigantic-scale tests end up introducing memory issues,
        # then we may want to manually run the garbage collector between test
        # iterations or between test runs.
        gc.disable()
        gc.set_threshold(0)

    if not global_cfg.log_cmds:
        return

    modules = [
        "ovsdbapp.backend.ovs_idl.transaction",
    ]
    for module_name in modules:
        logging.getLogger(module_name).setLevel(logging.DEBUG)


RESERVED = [
    'global',
    'cluster',
    'base_cluster_bringup',
    'ext_cmd',
]


def configure_tests(yaml, central_nodes, worker_nodes, global_cfg):
    tests = []
    for section, cfg in yaml.items():
        if section in RESERVED:
            continue

        mod = importlib.import_module(f'tests.{section}')
        class_name = ''.join(s.title() for s in section.split('_'))
        cls = getattr(mod, class_name)
        tests.append(cls(yaml, central_nodes, worker_nodes, global_cfg))
    return tests


def create_nodes(cluster_config, central, workers):
    node_az_conf = [
        NodeConf(
            cluster_config.node_net,
            DualStackSubnet.next(
                cluster_config.internal_net,
                i * (cluster_config.n_workers // cluster_config.n_az),
            ),
            DualStackSubnet.next(
                cluster_config.external_net,
                i * (cluster_config.n_workers // cluster_config.n_az),
            ),
            DualStackSubnet.next(
                cluster_config.gw_net,
                i * (cluster_config.n_workers // cluster_config.n_az),
            ),
            cluster_config.ts_net,
        )
        for i in range(cluster_config.n_az)
    ]

    db_containers = [
        [
            f'ovn-central-az{i+1}-1',
            f'ovn-central-az{i+1}-2',
            f'ovn-central-az{i+1}-3'
            if cluster_config.clustered_db
            else f'ovn-central-az{i+1}',
        ]
        for i in range(cluster_config.n_az)
    ]

    relay_containers = [
        [f'ovn-relay-az{i+1}-{j+1}' for j in range(cluster_config.n_relays)]
        for i in range(cluster_config.n_az)
    ]

    central_nodes = [
        CentralNode(
            central,
            db_containers[i],
            relay_containers[i],
            node_az_conf[i].mgmt_net.ip + 2,
            node_az_conf[i].gw_net,
            node_az_conf[i].ts_net,
            i,
        )
        for i in range(cluster_config.n_az)
    ]

    worker_nodes = [[] for _ in range(cluster_config.n_az)]
    for i in range(cluster_config.n_workers):
        az_index = i % cluster_config.n_az
        wn_index = i // cluster_config.n_az
        wn = WorkerNode(
            workers[i % len(workers)],
            f'ovn-scale-{i}',
            node_az_conf[az_index].mgmt_net.ip + 2,
            DualStackSubnet.next(
                node_az_conf[az_index].int_net,
                wn_index,
            ),
            DualStackSubnet.next(
                node_az_conf[az_index].ext_net,
                wn_index,
            ),
            node_az_conf[az_index].gw_net,
            i,
        )
        worker_nodes[az_index].append(wn)
    return central_nodes, worker_nodes


def set_ssl_keys(cluster_cfg):
    Stream.ssl_set_private_key_file(cluster_cfg.ssl_private_key)
    Stream.ssl_set_certificate_file(cluster_cfg.ssl_cert)
    Stream.ssl_set_ca_cert_file(cluster_cfg.ssl_cacert)


def prepare_test(central_nodes, worker_nodes, cluster_cfg, brex_cfg):
    if cluster_cfg.enable_ssl:
        set_ssl_keys(cluster_cfg)

    clusters = [
        Cluster(central_nodes[i], worker_nodes[i], cluster_cfg, brex_cfg)
        for i in range(len(central_nodes))
    ]
    with Context(
        clusters, 'prepare_test for cluster', len(central_nodes)
    ) as ctx:
        for i in ctx:
            clusters[i].start()

    return clusters


def run_base_cluster_bringup(clusters, bringup_cfg, global_cfg):
    if clusters[0].cluster_cfg.n_az > 1:
        clusters[0].create_transit_switch()

    with Context(
        clusters, 'base_cluster_bringup', clusters[0].cluster_cfg.n_az
    ) as ctx:
        for i in ctx:
            # create ovn topology
            ovn = clusters[i]
            ovn.create_cluster_router(f'lr-cluster{i + 1}')
            ovn.create_cluster_join_switch(f'ls-join{i + 1}')
            ovn.create_cluster_load_balancer(f'lb-cluster{i + 1}', global_cfg)
            if ovn.cluster_cfg.n_az > 1:
                ovn.connect_transit_switch()
            for worker in ovn.worker_nodes:
                worker.provision(ovn)
                ports = worker.provision_ports(
                    ovn, bringup_cfg.n_pods_per_node
                )
                worker.provision_load_balancers(ovn, ports, global_cfg)
                worker.ping_ports(ovn, ports)
            ovn.provision_lb_group(f'cluster-lb-group{i + 1}')

    if clusters[0].cluster_cfg.n_az > 1:
        clusters[0].check_ic_connectivity(clusters)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        usage(sys.argv[0])
        sys.exit(1)

    with open(sys.argv[2], 'r') as yaml_file:
        config = yaml.safe_load(yaml_file)

    global_cfg, cluster_cfg, brex_cfg, bringup_cfg = read_config(config)

    setup_logging(global_cfg)

    if not global_cfg.run_ipv4 and not global_cfg.run_ipv6:
        raise ovn_exceptions.OvnInvalidConfigException()

    central, workers = read_physical_deployment(sys.argv[1], global_cfg)
    central_nodes, worker_nodes = create_nodes(cluster_cfg, central, workers)
    tests = configure_tests(
        config,
        central_nodes,
        list(chain.from_iterable(worker_nodes)),
        global_cfg,
    )

    clusters = prepare_test(central_nodes, worker_nodes, cluster_cfg, brex_cfg)
    run_base_cluster_bringup(clusters, bringup_cfg, global_cfg)
    for test in tests:
        test.run(clusters, global_cfg)
    sys.exit(0)
