"""
Denoising autoencoders, single-layer and deep.
"""
import collections

import numpy as np
import matplotlib.pyplot as plt
import scipy.optimize

# os.environ['THEANO_FLAGS'] = 'device=gpu, floatX=float32'
# os.environ['THEANO_FLAGS'] = 'mode=DEBUG_MODE'
import theano
import theano.tensor as tt
import theano.sandbox.rng_mrg

from hinge import multi_hinge_margin
import plotting


def rms(x, **kwargs):
    return np.sqrt((x**2).mean(**kwargs))


def show_recons(x, z):
    plotting.compare([x.reshape(-1, 28, 28), z.reshape(-1, 28, 28)],
                     rows=5, cols=20, vlims=(-1, 2))


def sparse_mask(vis_shape, n_hid, rf_shape, rng=np.random):
    assert len(vis_shape) == 2 and len(rf_shape) == 2
    M, N = vis_shape
    m, n = rf_shape
    n_vis = M * N

    # find random positions for top-left corner of each RF
    i = rng.randint(low=0, high=M-m+1, size=n_hid)
    j = rng.randint(low=0, high=N-n+1, size=n_hid)

    mask = np.zeros((M, N, n_hid), dtype='bool')
    for k in xrange(n_hid):
        mask[i[k]:i[k]+m, j[k]:j[k]+n, k] = True

    return mask.reshape(n_vis, n_hid)


def split_params(param_vect, numpy_params):
    split = []
    i = 0
    for p in numpy_params:
        split.append(param_vect[i:i + p.size].reshape(p.shape))
        i += p.size
    return split


def join_params(param_arrays):
    return np.hstack([p.flatten() for p in param_arrays])


def shift_images(images, shape, r=1, rng=np.random):
    output = np.zeros_like(images)
    N = len(images)
    I = rng.randint(-r, r+1, N)
    J = rng.randint(-r, r+1, N)

    m, n = shape
    output = output.reshape((N, m, n))
    for k, image in enumerate(images):
        i, j = I[k], J[k]
        image = image.reshape(shape)
        output[k, max(i,0):min(m+i,m), max(j,0):min(n+j,n)] = (
            image[max(-i,0):min(m-i,m), max(-j,0):min(n-j,n)])

    return output.reshape(N, m*n)


class FileObject(object):
    """
    A object that can be saved to file
    """
    def to_file(self, file_name):
        d = {}
        d['__class__'] = self.__class__
        d['__dict__'] = self.__getstate__()
        np.savez(file_name, **d)

    @staticmethod
    def from_file(file_name):
        npzfile = np.load(file_name)
        cls = npzfile['__class__'].item()
        d = npzfile['__dict__'].item()

        self = cls.__new__(cls)
        self.__setstate__(d)
        return self


class Autoencoder(FileObject):
    """Autoencoder with tied weights"""

    def __init__(self, vis_shape, n_hid,
                 W=None, V=None, c=None, b=None, mask=None,
                 rf_shape=None, hid_func=None, vis_func=None, seed=22):
        dtype = theano.config.floatX

        self.vis_shape = vis_shape if isinstance(vis_shape, tuple) else (vis_shape,)
        self.n_vis = np.prod(vis_shape)
        self.n_hid = n_hid
        self.hid_func = hid_func
        self.vis_func = vis_func
        self.seed = seed

        rng = np.random.RandomState(seed=self.seed)
        self.theano_rng = theano.sandbox.rng_mrg.MRG_RandomStreams(seed=self.seed)

        # create initial weights and biases
        if W is None:
            Wmag = 4 * np.sqrt(6. / (self.n_vis + self.n_hid))
            W = rng.uniform(
                low=-Wmag, high=Wmag, size=(self.n_vis, self.n_hid)
            ).astype(dtype)

        if c is None:
            c = np.zeros(self.n_hid, dtype=dtype)

        if b is None:
            b = np.zeros(self.n_vis, dtype=dtype)

        # create initial sparsity mask
        self.rf_shape = rf_shape
        self.mask = mask
        if rf_shape is not None and mask is None:
            self.mask = sparse_mask(vis_shape, n_hid, rf_shape, rng=rng)

        if mask is not None:
            W = W * self.mask  # make initial W sparse
            if V is not None:
                V = V * self.mask.T

        # create states for weights and biases
        self.W = theano.shared(W.astype(dtype), name='W')
        self.c = theano.shared(c.astype(dtype), name='c')
        self.b = theano.shared(b.astype(dtype), name='b')
        if V is not None:
            self.V = theano.shared(V.astype(dtype), name='V')

    def __getstate__(self):
        d = dict(self.__dict__)
        for k, v in d.items():
            if k in ['W', 'V', 'c', 'b']:
                d[k] = v.get_value()
        return d

    def __setstate__(self, state):
        for k, v in state.items():
            if k in ['W', 'V', 'c', 'b']:
                self.__dict__[k] = theano.shared(v, name=k)
            else:
                self.__dict__[k] = v

    @property
    def filters(self):
        if self.mask is None:
            return self.W.get_value().T.reshape((self.n_hid,) + self.vis_shape)
        else:
            filters = self.W.get_value().T[self.mask.T]
            shape = (self.n_hid,) + self.rf_shape
            return filters.reshape(shape)

    def propup(self, x, noise=0):
        a = tt.dot(x, self.W) + self.c
        if noise > 0:
            a += self.theano_rng.normal(
                size=a.shape, std=noise, dtype=theano.config.floatX)
        return self.hid_func(a) if self.hid_func is not None else a

    def propdown(self, y):
        V = self.V if hasattr(self, 'V') else self.W.T
        a = tt.dot(y, V) + self.b
        return self.vis_func(a) if self.vis_func is not None else a

    @property
    def encode(self):
        data = tt.matrix('data')
        code = self.propup(data)
        return theano.function([data], code)

    @property
    def decode(self):
        code = tt.matrix('code')
        data = self.propdown(code)
        return theano.function([code], data)

    @property
    def reconstruct(self):
        data = tt.matrix('data')
        code = self.propup(data)
        recs = self.propdown(code)
        return theano.function([data], recs)

    def check_params(self):
        for param in [self.W, self.c, self.b]:
            if param is not None:
                assert np.isfinite(param.get_value()).all()

    def auto_sgd(self, images, deep=None, test_images=None,
                 batch_size=100, rate=0.1, noise=1., n_epochs=10):
        assert not hasattr(self, 'V')

        dtype = theano.config.floatX
        params = [self.W, self.c, self.b]

        # --- compute backprop function
        x = tt.matrix('images')
        xn = x + self.theano_rng.normal(size=x.shape, std=noise, dtype=dtype)
        y = self.propup(xn)
        z = self.propdown(y)

        # compute coding error
        rmses = tt.sqrt(tt.mean((x - z)**2, axis=1))
        error = tt.mean(rmses)

        # compute gradients
        grads = tt.grad(error, params)
        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        if self.mask is not None:
            updates[self.W] = updates[self.W] * self.mask

        train_dbn = theano.function([x], error, updates=updates)
        # reconstruct = deep.reconstruct if deep is not None else None
        encode = deep.encode if deep is not None else None
        decode = deep.decode if deep is not None else None

        # --- perform SGD
        batches = images.reshape(-1, batch_size, images.shape[1])
        assert np.isfinite(batches).all()

        for epoch in range(n_epochs):
            costs = []
            for batch in batches:
                costs.append(train_dbn(batch))
                self.check_params()

            print "Epoch %d: %0.3f" % (epoch, np.mean(costs))

            if deep is not None and test_images is not None:
                # plot reconstructions on test set
                plt.figure(2)
                plt.clf()
                test = test_images
                codes = encode(test)
                recs = decode(codes)
                # recons = reconstruct(test_images)
                show_recons(test, recs)
                plt.draw()

                print "Test set: (error: %0.3f) (sparsity: %0.3f)" % (
                    rms(test - recs, axis=1).mean(), (codes > 0).mean())

            # plot filters for first layer only
            if deep is not None and self is deep.autos[0]:
                plt.figure(3)
                plt.clf()
                plotting.filters(self.filters, rows=10, cols=20)
                plt.draw()


class DeepAutoencoder(object):

    def __init__(self, autos=None, seed=90, loss='hinge'):
        self.autos = autos if autos is not None else []
        self.W = None  # classifier weights
        self.b = None  # classifier biases

        self.seed = seed
        self.theano_rng = theano.sandbox.rng_mrg.MRG_RandomStreams(seed=self.seed)

        self.loss = loss

    def propup(self, images, noise=0):
        codes = images
        for auto in self.autos:
            codes = auto.propup(codes, noise=noise)
        return codes

    def propdown(self, codes):
        images = codes
        for auto in self.autos[::-1]:
            images = auto.propdown(images)
        return images

    def compute_loss(self, yc, y):
        if self.loss == 'nll':
            # compute negative log likelihood
            cost = -tt.mean(tt.log(tt.nnet.softmax(yc))[tt.arange(y.shape[0]), y])
            error = tt.mean(tt.neq(tt.argmax(yc, axis=1), y))
        elif self.loss == 'hinge':
            # compute hinge loss
            cost = multi_hinge_margin(yc, y).mean()
            error = tt.mean(tt.neq(tt.argmax(yc, axis=1), y))
        else:
            raise ValueError("Unrecognized loss type '%s'" % self.loss)

        return cost, error

    @property
    def encode(self):
        images = tt.matrix('images')
        codes = self.propup(images)
        return theano.function([images], codes)

    @property
    def decode(self):
        codes = tt.matrix('codes')
        images = self.propdown(codes)
        return theano.function([codes], images)

    @property
    def reconstruct(self):
        x = tt.matrix('images')
        y = self.propup(x)
        z = self.propdown(y)
        f = theano.function([x], z)
        return f

    def auto_sgd(self, images, test_images=None,
                 batch_size=100, rate=0.1, n_epochs=10):
        """Adjust tied weights to be a better autoencoder"""
        dtype = theano.config.floatX

        params = []
        for auto in self.autos:
            params.extend((auto.W, auto.c, auto.b))

        # --- compute backprop function
        x = tt.matrix('images')
        xn = x + self.theano_rng.normal(size=x.shape, std=1, dtype=dtype)

        # compute coding error
        y = self.propup(xn)
        z = self.propdown(y)
        rmses = tt.sqrt(tt.mean((x - z)**2, axis=1))
        error = tt.mean(rmses)

        # compute gradients
        grads = tt.grad(error, params)
        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        for auto in self.autos:
            if auto.mask is not None:
                updates[auto.W] = updates[auto.W] * auto.mask

        train_dbn = theano.function([x], error, updates=updates)
        reconstruct = self.reconstruct

        # --- perform SGD
        batches = images.reshape(-1, batch_size, images.shape[1])
        assert np.isfinite(batches).all()

        for epoch in range(n_epochs):
            costs = []
            for batch in batches:
                costs.append(train_dbn(batch))
                # self.check_params()

            print "Epoch %d: %0.3f" % (epoch, np.mean(costs))

            if test_images is not None:
                # plot reconstructions on test set
                plt.figure(2)
                plt.clf()
                recons = reconstruct(test_images)
                show_recons(test_images, recons)
                plt.draw()

            # plot filters for first layer only
            plt.figure(3)
            plt.clf()
            plotting.filters(self.autos[0].filters, rows=10, cols=20)
            plt.draw()

    def auto_sgd_down(self, images, test_images=None,
                      batch_size=100, rate=0.1, n_epochs=10):
        """Adjust generative weights to be a better autoencoder

        This should not affect the classification accuracy of the system.
        """
        dtype = theano.config.floatX

        params = []
        for auto in self.autos:
            auto.V = theano.shared(auto.W.get_value(borrow=False).T, name='V')
            params.extend((auto.V, auto.b))

        # --- compute backprop function
        x = tt.matrix('images')
        xn = x + self.theano_rng.normal(size=x.shape, std=1, dtype=dtype)

        # compute coding error
        y = self.propup(xn)
        z = self.propdown(y)
        rmses = tt.sqrt(tt.mean((x - z)**2, axis=1))
        error = tt.mean(rmses)

        # compute gradients
        grads = tt.grad(error, params)
        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        for auto in self.autos:
            if auto.mask is not None:
                updates[auto.V] = updates[auto.V] * auto.mask.T

        train_dbn = theano.function([x], error, updates=updates)
        reconstruct = self.reconstruct

        # --- perform SGD
        batches = images.reshape(-1, batch_size, images.shape[1])
        assert np.isfinite(batches).all()

        for epoch in range(n_epochs):
            costs = []
            for batch in batches:
                costs.append(train_dbn(batch))
                # self.check_params()

            print "Epoch %d: %0.3f" % (epoch, np.mean(costs))

            if test_images is not None:
                # plot reconstructions on test set
                plt.figure(2)
                plt.clf()
                recons = reconstruct(test_images)
                show_recons(test_images, recons)
                plt.draw()

            # plot filters for first layer only
            plt.figure(3)
            plt.clf()
            plotting.filters(self.autos[0].filters, rows=10, cols=20)
            plt.draw()

    def train_classifier(self, train, test, n_epochs=30):
        dtype = theano.config.floatX

        # --- find codes
        images, labels = train
        n_labels = len(np.unique(labels))
        print("Train classifier: n_labels=%s" % n_labels)
        codes = self.encode(images.astype(dtype))

        codes = theano.shared(codes.astype(dtype), name='codes')
        labels = tt.cast(theano.shared(labels.astype(dtype), name='labels'), 'int32')

        # --- compute backprop function
        Wshape = (self.autos[-1].n_hid, n_labels)
        x = tt.matrix('x', dtype=dtype)
        y = tt.ivector('y')
        W = tt.matrix('W', dtype=dtype)
        b = tt.vector('b', dtype=dtype)

        # compute gradients
        cost, _ = self.compute_loss(tt.dot(x, W) + b, y)
        grads = tt.grad(cost, [W, b])
        f_df = theano.function(
            [W, b], [cost] + grads,
            givens={x: codes, y: labels})

        # --- begin backprop
        W0 = np.random.normal(size=Wshape).astype(dtype).flatten() / 10
        b0 = np.zeros(n_labels)

        split_p = lambda p: [p[:-n_labels].reshape(Wshape), p[-n_labels:]]
        form_p = lambda params: np.hstack([p.flatten() for p in params])

        def f_df_wrapper(p):
            w, b = split_p(p)
            outs = f_df(w.astype(dtype), b.astype(dtype))
            cost, grad = outs[0], form_p(outs[1:])
            return cost.astype('float64'), grad.astype('float64')

        p0 = form_p([W0, b0])
        p_opt, mincost, info = scipy.optimize.lbfgsb.fmin_l_bfgs_b(
            f_df_wrapper, p0, maxfun=n_epochs, iprint=1)

        self.W, self.b = split_p(p_opt)

    def lbfgs(self, train_set, test_set, shift=False, n_epochs=30):
        dtype = theano.config.floatX

        params = []
        for auto in self.autos:
            params.extend([auto.W, auto.c])

        # --- compute backprop function
        assert self.W is not None and self.b is not None
        W = theano.shared(self.W.astype(dtype), name='Wc')
        b = theano.shared(self.b.astype(dtype), name='bc')

        x = tt.matrix('batch')
        y = tt.ivector('labels')

        # compute classification error
        yn = self.propup(x)
        cost, _ = self.compute_loss(tt.dot(yn, W) + b, y)

        # compute gradients
        grads = tt.grad(cost, params)
        f_df = theano.function([x, y], [cost] + grads)

        np_params = [param.get_value() for param in params]

        # --- run L_BFGS
        train_images, train_labels = train_set
        train_labels = train_labels.astype('int32')

        def f_df_wrapper(p):
            for param, value in zip(params, split_params(p, np_params)):
                param.set_value(value.astype(param.dtype))

            images = shift_images(train_images, (28, 28)) if shift else train_images
            labels = train_labels

            outs = f_df(images, labels)
            cost, grads = outs[0], outs[1:]
            grad = join_params(grads)
            return cost.astype('float64'), grad.astype('float64')

        p0 = join_params(np_params)
        p_opt, mincost, info = scipy.optimize.lbfgsb.fmin_l_bfgs_b(
            f_df_wrapper, p0, maxfun=n_epochs, iprint=1)

        for param, value in zip(params, split_params(p_opt, np_params)):
            param.set_value(value.astype(param.dtype), borrow=False)

    def sgd(self, train_set, test_set,
            rate=0.1, noise=0, shift=False, tradeoff=0.5, n_epochs=30, batch_size=100):
        """Use SGD to do combined autoencoder and classifier training"""
        dtype = theano.config.floatX
        assert tradeoff >= 0 and tradeoff <= 1

        params = []
        for auto in self.autos:
            auto.V = theano.shared(auto.W.get_value(borrow=False).T, name='V')
            params.extend([auto.W, auto.V, auto.c, auto.b])

        # --- compute backprop function
        assert self.W is not None and self.b is not None
        W = theano.shared(self.W.astype(dtype), name='Wc')
        b = theano.shared(self.b.astype(dtype), name='bc')

        x = tt.matrix('batch')
        y = tt.ivector('labels')

        xn = x
        # xn = x + self.theano_rng.normal(size=x.shape, std=0.1, dtype=dtype)
        yn = self.propup(xn, noise=noise)
        class_cost, class_error = self.compute_loss(tt.dot(yn, W) + b, y)

        # compute autoencoder error
        z = self.propdown(yn)
        rmses = tt.sqrt(tt.mean((x - z)**2, axis=1))
        auto_cost = tt.mean(rmses)

        cost = (tt.cast(1 - tradeoff, dtype) * auto_cost
                + tt.cast(tradeoff, dtype) * class_cost)
        error = class_error

        # compute gradients
        grads = tt.grad(cost, params)
        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        for auto in self.autos:
            if auto.mask is not None:
                updates[auto.W] = updates[auto.W] * auto.mask
                updates[auto.V] = updates[auto.V] * auto.mask.T

        train_dbn = theano.function([x, y], error, updates=updates)
        reconstruct = self.reconstruct

        # --- perform SGD
        train_images, train_labels = train_set
        train_labels = train_labels.astype('int32')
        test_images, test_labels = test_set

        for epoch in range(n_epochs):
            images = shift_images(train_images, (28, 28)) if shift else train_images
            labels = train_labels
            ibatches = images.reshape(-1, batch_size, images.shape[1])
            lbatches = labels.reshape(-1, batch_size)

            costs = []
            for batch, label in zip(ibatches, lbatches):
                costs.append(train_dbn(batch, label))

            # copy back parameters (for test function)
            self.W = W.get_value()
            self.b = b.get_value()

            print "Epoch %d: %0.4f" % (epoch, np.mean(costs))

            if test_images is not None:
                # plot reconstructions on test set
                plt.figure(2)
                plt.clf()
                recons = reconstruct(test_images)
                show_recons(test_images, recons)
                plt.draw()

            # plot filters for first layer only
            plt.figure(3)
            plt.clf()
            plotting.filters(self.autos[0].filters, rows=10, cols=20)
            plt.draw()

    def test(self, test_set):
        assert self.W is not None and self.b is not None

        images, labels = test_set
        codes = self.encode(images)

        categories = np.unique(labels)
        inds = np.argmax(np.dot(codes, self.W) + self.b, axis=1)
        return (labels != categories[inds])


def test_autoencoder():
    [train_images, _], _, _ = mnist()
    normalize(train_images)

    f = tt.nnet.sigmoid
    auto = Autoencoder((28, 28), 200, rf_shape=(9, 9), hid_func=f)
    auto.auto_sgd(train_images, rate=1.0, noise=1, n_epochs=3)
    # auto.auto_sgd(train_images, rate=0.1, noise=0, n_epochs=3)

    test = train_images[:1000]
    recs = auto.reconstruct(test)
    print rms(test - recs, axis=1).mean()

    plt.figure(101)
    plt.clf()
    show_recons(test, recs)
    # plt.show()

    auto.to_file('auto.npz')

    auto2 = FileObject.from_file('auto.npz')
    recs2 = auto2.reconstruct(test)
    print rms(test - recs2, axis=1).mean()

    plt.figure(102)
    plt.clf()
    show_recons(test, recs)
    plt.show()


def test_shift_images():
    # n = 5
    # images = np.zeros((n, 9))
    # shape = (3, 3)
    # images[np.arange(n), 4] = 1

    # images2 = shift_images(images, shape)

    # for image in images2:
    #     print image.reshape(shape)

    # --- timing test
    from hunse_tools.timing import tic, toc
    [train_images, _], _, _ = mnist()

    tic()
    images2 = shift_images(train_images, (28, 28))
    toc()

if __name__ == '__main__':
    # test_autoencoder()
    test_shift_images()
