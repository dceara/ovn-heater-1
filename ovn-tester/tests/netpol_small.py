from tests.netpol import NetPol


class NetpolSmall(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super(NetpolSmall, self).__init__(
            'netpol_small', config, central_node, worker_nodes
        )

    def run(self, clusters, global_cfg):
        self.init(clusters, global_cfg)
        super(NetpolSmall, self).run(clusters, global_cfg, True)
