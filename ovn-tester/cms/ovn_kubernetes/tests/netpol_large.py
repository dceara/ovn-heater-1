from cms.ovn_kubernetes.tests.netpol import NetPol


class NetpolLarge(NetPol):
    def __init__(self, config, clusters, global_cfg):
        super().__init__('netpol_large', config, clusters)

    def run(self, clusters, global_cfg):
        self.init(clusters, global_cfg)
        super().run(clusters, global_cfg)
