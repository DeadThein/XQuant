# -*- coding: utf-8 -*-

"""
Bayesian Optimization
利用高斯随机过程（Gaussian Process）进行贝叶斯优化

参考：
学术文献：https://arxiv.org/pdf/1206.2944.pdf
算法实现：https://github.com/fmfn/BayesianOptimization

@author: X0Leon
"""

import numpy as np
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern
from scipy.optimize import minimize
from scipy.stats import norm


class UtilityFunction(object):
    """
    计算acquisition function，即下一次迭代选择的依据
    """

    def __init__(self, kind, kappa, xi):
        """
        UCB（Upper Confidence Bound）需要kappa参数
        """
        self.kappa = kappa
        self.xi = xi
        if kind not in ['ucb', 'ei', 'poi']:
            raise NotImplementedError('Not implemented utility function {}'.format(kind))
        else:
            self.kind = kind

    def utility(self, x, gp, y_max):
        if self.kind == 'ucb':
            return self._ubc(x, gp, self.kappa)
        if self.kind == 'ei':
            return self._ei(x, gp, y_max, self.xi)
        if self.kind == 'poi':
            return self._poi(x, gp, y_max, self.xi)

    @staticmethod
    def _ubc(x, gp, kappa):
        """
        GP Upper Confidence Bound, (Srinivas, 2010)
        """
        mean, std = gp.predict(x, return_std=True)
        return mean + kappa * std

    @staticmethod
    def _ei(x, gp, y_max, xi):
        """
        Expected Improvement, (Mockus, 1978)
        """
        mean, std = gp.predict(x, return_std=True)
        z = (mean - y_max - xi) / std
        return (mean - y_max - xi) * norm.cdf(z) + std * norm.pdf(z)

    @staticmethod
    def _poi(x, gp, y_max, xi):
        """
        Probability of Improvement, (Kushner, 1964)
        """
        mean, std = gp.predict(x, return_std=True)
        z = (mean - y_max - xi) / std
        return norm.cdf(z)


def unique_rows(a):
    """
    剔除优化过程中重复出现的rows
    这在使用sklearn GP 对象优化中是必要的
    参数：
    a: 需要剔除重复行的array
    返回:
    独一无二的行的掩膜（mask）
    """
    order = np.lexsort(a.T)
    reorder = np.argsort(order)

    a = a[order]
    diff = np.diff(a, axis=0)
    ui = np.ones(len(a), 'bool')
    ui[1:] = (diff != 0).any(axis=1)

    return ui[reorder]


def acq_max(ac, gp, y_max, bounds):
    """
    用来寻找acquisition function最大值的函数，算法：L-BFGS-B
    参数：
    ac: acquisition function对象，返回逐点值
    gp: 高斯过程，拟合相关的数据点
    y_max: 目标函数的当前已知最大值
    bounds: acq max寻找范围的边界
    返回：
    x_max: acquisition function最大时的x (arg max)
    """
    x_max = bounds[:, 0]  # 从下界开始搜索
    max_acq = None

    x_tries = np.random.uniform(bounds[:, 0], bounds[:, 1], size=(100, bounds.shape[0]))

    for x_try in x_tries:
        # 寻找acq函数的最大值，也即取负后的最小值
        res = minimize(lambda x: -ac(x.reshape(1, -1), gp=gp, y_max=y_max),
                       x_try.reshape(1, -1), bounds=bounds, method='L-BFGS-B')

        if max_acq is None or -res.fun >= max_acq:
            x_max = res.x
            max_acq = -res.fun
    # 由于使用浮点，要确保x_max位于bounds范围内
    return np.clip(x_max, bounds[:, 0], bounds[:, 1])


class BayesianOptimization(object):
    def __init__(self, f, pbounds):
        """
        参数：
        f: 需要最大化的函数，black-box
        pbounds: 字典，key为参数名称，value为最大最小值的tuple
        """
        self.pbounds = pbounds
        self.keys = list(pbounds.keys())
        self.dim = len(pbounds)
        self.bounds = []
        for key in self.pbounds.keys():
            self.bounds.append(self.pbounds[key])
        self.bounds = np.asarray(self.bounds)
        self.f = f

        self.initialized = False
        self.init_points = []
        self.x_init = []
        self.y_init = []

        self.X = None
        self.Y = None

        # 迭代次数i
        self.i = 0

        # scikit-learn中的GaussianProcess
        self.gp = GaussianProcessRegressor(kernel=Matern(), n_restarts_optimizer=25)

        # Utility函数
        self.util = None
        # 输出字典
        self.res = dict()
        self.res['max'] = {'max_val': None,
                           'max_params': None}
        self.res['all'] = {'values': [], 'params': []}

    def init(self, init_points):
        """
        初始化优化过程，既包括用户输入的数据，也随机生成一些
        参数：
        init_points: 随机产生数据点的数目
        """
        # 生成随机点
        l = [np.random.uniform(x[0], x[1], size=init_points) for x in self.bounds]

        # 合并随机产生的点和可能存在的从self.explore方法中输入的点
        self.init_points += list(map(list, zip(*l)))

        # 用list存储函数新产生的值
        y_init = []

        # 计算目标函数在所以初始化点的值(random + explore)
        for x in self.init_points:
            y_init.append(self.f(**dict(zip(self.keys, x))))

        # 添加其他由用户通过self.initialize方法传入的数据点和相应的函数值
        self.init_points += self.x_init

        # 添加由self.initialize传入的目标函数值
        y_init += self.y_init

        self.X = np.asarray(self.init_points)
        self.Y = np.asarray(y_init)

        self.initialized = True

    def explore(self, points_dict):
        """
        搜寻用户自定义的点
        points_dict: 参数名称和值的字典
        """
        # Consistency check每个参数的数目应该相同
        param_tup_lens = []

        for key in self.keys:
            param_tup_lens.append(len(list(points_dict[key])))

        if all([e == param_tup_lens[0] for e in param_tup_lens]):
            pass
        else:
            raise ValueError('Number of initialization points for every parameter must be same.')

        # 列表的列表：2D
        all_points = []
        for key in self.keys:
            all_points.append(points_dict[key])

        # 转置列表的列表，如[[1,2],[3,4]] -> [[1,3],[2,4]]
        self.init_points = list(map(list, zip(*all_points)))

    def initialize(self, points_dict):
        """
        给出目标值已知的那些数据点
        """

        for target in points_dict:

            self.y_init.append(target)

            all_points = []
            for key in self.keys:
                all_points.append(points_dict[target][key])

            self.x_init.append(all_points)

    def set_bounds(self, new_bounds):
        """
        改变搜索区域上界和下界的功能函数
        参数：
        new_bounds: 参数名称和新的边界的字典
        """
        self.pbounds.update(new_bounds)

        for row, key in enumerate(self.pbounds.keys()):
            self.bounds[row] = self.pbounds[key]

    def maximize(self, init_points=5, n_iter=25, acq='ei', kappa=2.576, xi=0.0, **gp_params):
        """
        主要的优化方法
        参数：
        init_points: 随机选择的点的数目，用于在GP拟合前对目标函数采点
        n_iter: 迭代总次数，目前没有停止迭代的判据，所以必须指定
        acq: Acquisition function, 默认是Expected Improvement.
        gp_params: 传给scikit-learn Gaussian Process对象的参数
        """
        # acquisition function
        self.util = UtilityFunction(kind=acq, kappa=kappa, xi=xi)

        # 初始化x, y，找到当前的y_max
        if not self.initialized:
            self.init(init_points)

        y_max = self.Y.max()

        # 接受可能传入的参数
        self.gp.set_params(**gp_params)

        # 找到独特的rows，防止GP被中断
        ur = unique_rows(self.X)
        self.gp.fit(self.X[ur], self.Y[ur])

        # 寻找acquisition function最大时的参数，argmax
        x_max = acq_max(ac=self.util.utility, gp=self.gp, y_max=y_max, bounds=self.bounds)
        # 迭代寻优
        for i in range(n_iter):
            # 测试x_max是否重复，如果是，则随机重挑一个
            if np.any((self.X - x_max).sum(axis=1) == 0):
                x_max = np.random.uniform(self.bounds[:, 0],
                                          self.bounds[:, 1],
                                          size=self.bounds.shape[0])

            # 添加最新产生的值到X和Y数组中
            self.X = np.vstack((self.X, x_max.reshape((1, -1))))
            self.Y = np.append(self.Y, self.f(**dict(zip(self.keys, x_max))))

            # 更新GP
            ur = unique_rows(self.X)
            self.gp.fit(self.X[ur], self.Y[ur])

            # 更新最大值，用于下一次寻找
            if self.Y[-1] > y_max:
                y_max = self.Y[-1]

            # 最大化acquisition function用于下一次寻找
            x_max = acq_max(ac=self.util.utility,
                            gp=self.gp,
                            y_max=y_max,
                            bounds=self.bounds)

            # 迭代次数的追踪
            self.i += 1

            self.res['max'] = {'max_val': self.Y.max(),
                               'max_params': dict(zip(self.keys, self.X[self.Y.argmax()]))
                               }
            self.res['all']['values'].append(self.Y[-1])
            self.res['all']['params'].append(dict(zip(self.keys, self.X[-1])))


if __name__ == '__main__':
    import time
    start = time.time()
    # 使用示例
    # 传入需要最大化的函数和其参数的范围来创建BO对象
    # 这里用简单的二次函数，假装我们不知道其形式
    bo = BayesianOptimization(lambda x, y: -x ** 2 - (y - 1) ** 2 + 1, {'x': (-4, 4), 'y': (-3, 3)})
    # 输入我们想要BO算法计算的值，参数为key、参数值为value的字典
    bo.explore({'x': [-1, 3], 'y': [-2, 2]})
    # 如果有先验的信息，即使不准确（如-2和-1.251），也一并丢给BO优化器
    bo.initialize({-2: {'x': 1, 'y': 0}, -1.251: {'x': 1, 'y': 1.5}})

    # 做好上面的各种初始化工作，我们就可以调用maximize方法来优化！
    # 注意：总的函数运算次数要大于优化迭代次数，因为初始化时调用函数计算random+explore个数据点
    bo.maximize(init_points=5, n_iter=15, kappa=3.29)
    # 最大值存在self.res中
    print(bo.res['max'])

    ################
    # 如果我们不是很满意，增加点需要优化的值，改改参数，继续优化
    bo.explore({'x': [0.6], 'y': [-0.23]})
    # 修改高斯过程会大大改变优化行为
    gp_params = {'kernel': None,
                 'alpha': 1e-5}
    # 使用不同的acquisition function
    bo.maximize(n_iter=5, acq='ei', **gp_params)
    print(bo.res['max'])
    print(bo.res['all'])

    end = time.time()
    print(end-start)