from tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config, central_node, worker_nodes, global_cfg):
        super(NetpolLarge, self).__init__(
            'netpol_large', config, central_node, worker_nodes
        )

    def run(self, clusters, global_cfg):
        self.init(clusters, global_cfg)
        super(NetpolLarge, self).run(clusters, global_cfg)
