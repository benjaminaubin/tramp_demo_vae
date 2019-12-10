import numpy as np
import random
import h5py
import time
import copy
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.sparse.linalg import eigs, eigsh
from numpy.linalg import eig, svd
from scipy import stats

# Tramp package
from tramp.algos.metrics import mean_squared_error, overlap
from tramp.experiments import run_experiments, qplot
from tramp.ensembles import GaussianEnsemble, ComplexGaussianEnsemble, MarchenkoPasturEnsemble
from tramp.variables import SISOVariable as V, SILeafVariable as O, MISOVariable as M
from tramp.likelihoods import GaussianLikelihood
from tramp.priors import GaussBernouilliPrior, GaussianPrior
from tramp.channels import LinearChannel, AbsChannel, AnalyticalLinearChannel, ReshapeChannel, GaussianChannel, LeakyReluChannel,ReluChannel, SgnChannel, LowRankFactorization, LowRankGramChannel, HardTanhChannel, HardSigmoidChannel, BiasChannel
from tramp.experiments import BayesOptimalScenario
from tramp.algos import CustomInit, ConstantInit, NoisyInit
from tramp.algos import JoinCallback, EarlyStopping,ExpectationPropagation, StateEvolution
 
# Specific to keras
from keras.datasets import mnist, fashion_mnist
from keras.utils import normalize

# Logging
import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Model_Prior():
    """
    Implements EP algorithm for model:

    y = model(x_star) + sqrt(Delta) * xi
        - denoising model(x_star) = x_star
        - inpainting
    where:
        - x_star :
            - mnist
            - fashion mnist

    Student trained with EP, with a structured prior
    - y = model(x) + sqrt(Delta) * xi
        with x ~
                - VAE
    """

    def __init__(self, model_params={'name': 'low_rank', 'type': 'xx', 'K': 1, 'N': 1000, 'alpha': 1},
                 data_params={'name': 'gaussian'}, prior_params={'name': 'gaussian'}, Delta=0.5, seed=False, daft=False,
                 plot_truth=False, plot_truth_vs_pred=False, plot_prior_sample=False):

        # Model properties
        self.model_params = model_params
        self.N = model_params['N']
        self.Delta = Delta

        # Data params
        self.data_params = data_params

        # Prior params
        self.prior_params = prior_params

        # Seed
        self.seed = seed
        if self.seed != 0:
            np.random.seed(self.seed)

        # Daft model
        self.daft = daft

        # Plot sample
        self.plot_truth_vs_pred = plot_truth_vs_pred
        self.plot_truth = plot_truth
        self.plot_prior_sample = plot_prior_sample

        # Damping variables
        self.list_var = []

        # Callback
        self.callback = EarlyStopping(tol=1e-4, min_variance=1e-12)

    def setup(self):
        # Build prior module
        prior_x = self.build_prior()

        # Sample from the prior
        self.sample_from_prior(prior_x)
            
        # Init the model
        model = self.init_model(prior_x)

        # Generate sample from dataset
        y = self.generate_sample()

        # Build the model with likelihood on y and prior_x on x
        model_ = self.build_model(model, y)
        self.model = model_.to_model()

        # Daft the model
        if self.daft:
            self.model.daft()
            plt.show(block=False)
            input("...")
            plt.close()

    def init_model(self, prior_x):
        self.y_ids = ['y']

        if self.model_params['name'] == 'denoising':
            # Model
            model = prior_x @ V(id="x")
            # Variables
            self.x_ids = ['x']
            self.list_var.extend(['x'])

        elif self.model_params['name'] == 'inpainting':
            # Create sensing matrix 
            N_rem = self.model_params['N_rem']
            N = self.model_params['N']
            F = np.identity(N)

            ## Remove a Band ##
            if self.model_params['mode'] == 'band':
                id_0 = int(N/2) - int(N_rem/2)
                for rem in range(id_0,id_0+N_rem):
                    F[rem,rem] = 0
            
            ## Remove randomly ##
            if self.model_params['mode'] == 'random':
                for i in range(N_rem):
                    rem = random.randrange(1, N, 1)

                    F[rem,rem] = 0

            ## Diagonal ##
            if self.model_params['mode'] == 'diagonal':
                l = 4
                for j in range(-int(l/2),int(l/2),1):
                    for i in range(1, 27, 1):
                        ind = i * 28 + i + j  
                        F[ind,ind] = 0
                        ind = i * 28 - i - j 
                        F[ind,ind] = 0

            F_tot = F
            F_obs =  np.delete(F,np.where(~F.any(axis=0))[0], axis=0)

            self.F = F_obs
            self.F_tot = F_tot
            # Model
            model = prior_x @ V(id="x") @ LinearChannel(
                F_obs, name="F") @ V(id="z")
        
            # Variables
            self.x_ids = ['x']
            self.list_var.extend(['x'])

        else:
            raise NotImplementedError

        return model

    def channel(self, x):
        # denoising
        if self.model_params['name'] == 'denoising':
            noise = np.sqrt(self.Delta) * np.random.randn(self.N)
            y = x + noise

        # inpainting
        elif self.model_params['name'] == 'inpainting':
            y = self.F @ x
            self.y_inp = self.F_tot @ x

        return y

    def sample_from_prior(self, prior_x):
        if self.plot_prior_sample :
            model = prior_x @ O(id="y")
            model = model.to_model_dag()
            prior_sample = model.to_model()
            fig, axs = plt.subplots(5, 5, figsize=(8, 8))
            for ax in axs.ravel():
                sample = prior_sample.sample()['y']
                #print('min:',np.min(sample), 'max:',np.max(sample))
                ax.set_axis_off()
                ax.imshow(sample.reshape(28, 28), cmap="gray")
            
            file_name = f"./Images/Prior_{self.prior_params['name']}_{self.prior_params['type']}_{self.prior_params['id']}.png"
            plt.savefig(file_name, format='png', dpi=1000,
                    bbox_inches="tight", pad_inches=0.1)
            plt.show(block=False)
            #input("...")
            plt.close()

    def generate_sample(self):
        self.x_true, self.y_true = {}, {}

        if self.data_params['name'] == 'gaussian':
            x_star = np.random.randn(self.N)

        elif self.data_params['name'] in ['mnist', 'fashion_mnist']:
            assert self.N == 784
            if self.data_params['name'] == 'mnist':
                (_, _), (X_test, _) = mnist.load_data()
            else:
                (_, _), (X_test, _) = fashion_mnist.load_data()

            # Transform data
            X_test_spec = 2 * (X_test / 255) - 1.
            X_test_spec = X_test_spec.reshape(
                10000, 784)-np.sum(X_test_spec.reshape(10000, 784), 1).reshape(10000, 1)/784
            X_test_spec = normalize(
                X_test_spec, axis=-1, order=2) * np.sqrt(784)

            X_test_ep = 2 * (X_test / 255) - 1.

            # Draw random sample
            #id = np.random.randint(0, X_test.shape[0], 1)
            id = self.seed
            
            # Choose x_star
            x_star = X_test_ep[id].reshape(self.N)
            x_star_spec = X_test_spec[id].reshape(self.N)

            if self.plot_truth:
                fig, ax = plt.subplots(1, 1, figsize=(8, 8))
                ax.imshow(x_star.reshape(28, 28), cmap='Greys')
                plt.show(block=False)
                input('...')
                plt.close()

        elif self.data_params['name'] == 'GAN':
            prior_x = self.build_GAN_prior(self.data_params)
            model_GAN = (prior_x @ O(id='x')).to_model_dag()
            teacher = DAGModel(model_GAN)
            sample = teacher.sample()
            x_star = sample['x']

            if self.plot_truth:
                fig, axs = plt.subplots(5, 5, figsize=(8, 8))
                for ax in axs.ravel():
                    sample = teacher.sample()
                    ax.set_axis_off()
                    ax.imshow(sample['x'].reshape(28, 28), cmap='Greys')
                plt.show(block=False)
                input('...')
                plt.close()

        else:
            raise NotImplementedError

        y = self.channel(x_star)
        self.x_true['x'] = x_star
        self.y_true['y'] = y
        y_spec = self.channel(x_star_spec)
        self.x_true['x_spec'] = x_star_spec
        self.y_true['y_spec'] = y_spec

        if self.plot_truth and self.model_params['name'] == 'inpainting':
            fig, ax = plt.subplots(1, 1, figsize=(8, 8))
            ax.imshow(y.reshape(28, 28), cmap='Greys')
            plt.show(block=False)
            input('...')
            plt.close()
        return y

    def build_prior(self):
        self.shape = (
            self.N, 1) if self.model_params['name'] == 'low_rank' else (self.N)
        # Gaussian prior
        if self.prior_params['name'] == 'gaussian':
            # prior_params = {'name'}s
            prior_x = GaussianPrior(size=self.shape)

        # GLM prior with channel
        elif self.prior_params['name'] == 'GLM':
            # prior_params = {'name', 'alpha', 'channel'}
            alpha = self.prior_params['alpha']
            D = int(self.N / alpha)
            W = GaussianEnsemble(self.N, D).generate()
            prior_x = GaussianPrior(size=D) @ V(
                id="z0") @ LinearChannel(W) @ V(id="Wz0")
            self.list_var.extend(['z0', 'Wz0'])

            if self.prior_params['channel'] == 'linear':
                prior_x = prior_x

            elif self.prior_params['channel'] == 'sign':
                prior_x = prior_x @ SngChannel() @ V(id="s")
                self.list_var.extend(['s'])

            elif self.prior_params['channel'] == 'relu':
                prior_x = prior_x @ ReluChannel() @ V(id="s")
                self.list_var.extend(['s'])

            else:
                raise NotImplementedError
            prior_x = prior_x @ ReshapeChannel(prev_shape=self.N, next_shape=self.shape)

        # Multi-layer prior
        elif self.prior_params['name'] == 'ML':
            # prior_params = {'name', 'alphas', 'channel'}
            tab_alphas = self.prior_params['alphas']
            n_layers = len(tab_alphas)
            tab_N, tab_W, tab_b = [self.N], [], []
            # Compute layers sizes
            for alpha in tab_alphas[::-1]:
                M = int(N / alpha)
                N = M
                tab_N.append(M)
            print(tab_N)
            # Generate weights
            for i in range(n_layers):
                W = GaussianEnsemble(tab_N[i], tab_N[i+1]).generate()
                tab_W.append(W)
            print([tab_W[i].shape for i in range(n_layers)])
            # Generate bias
            for i in range(n_layers):
                tab_b.append(np.random.random(tab_N[i]))

            prior_x = GaussianPrior(size=tab_N[-1]) @ V(id=f"z0")
            self.list_var.append('z0')
            for i in range(n_layers):
                prior_x = prior_x @ LinearChannel(tab_W[-1-i]) @ V(id=f"t_{i+1}") @ BiasChannel(
                    tab_b[-1-i]) @ V(id=f"u_{i+1}") @  ReluChannel() @ V(id=f"a_{i+1}")
                self.list_var.extend([f"t_{i+1}", f"u_{i+1}", f"a_{i+1}"])

            prior_x = prior_x @ ReshapeChannel(prev_shape=self.N, next_shape=self.shape)


        # VAE prior
        elif self.prior_params['name'] == 'VAE':
            prior_x = self.build_VAE_prior(self.prior_params)

        return prior_x

    def build_VAE_prior(self, params):
        shape = self.shape
        print(params['id'])

        assert self.N == 784
        biases, weights = self.load_VAE_prior(params)

        if params['id'] == '20_relu_400_sigmoid_784_bias':
            D, N1, N = 20, 400, 28*28
            W1, W2 = weights
            b1, b2 = biases
            prior_x = (GaussianPrior(size=D) @ V(id="z_0") @
                       LinearChannel(W1, name="W_1") @ V(id="Wz_1") @ BiasChannel(b1) @ V(id="b_1") @ LeakyReluChannel(0) @ V(id="z_1") @
                       LinearChannel(W2, name="W_2") @ V(id="Wz_2") @ BiasChannel(b2) @ V(id="b_2") @ HardTanhChannel() @ V(id="z_2") @
                       ReshapeChannel(prev_shape=self.N, next_shape=self.shape))
            self.list_var.extend(
                ['z_0', 'Wz_1', 'Wz_2', 'z_1', 'z_2', 'b_1', 'b_2'])

        elif params['id'] == '20_relu_400_sigmoid_784_old' or params['id'] == '20_relu_400_sigmoid_784' :
            D, N1, N = 20, 400, 28*28
            W1, W2 = weights
            prior_x = (GaussianPrior(size=D) @ V(id="z_0") @
                       LinearChannel(W1, name="W_1") @ V(id="Wz_1") @ LeakyReluChannel(0) @ V(id="z_1") @
                       LinearChannel(W2, name="W_2") @ V(id="Wz_2") @ HardTanhChannel() @ V(id="z_2") @
                       ReshapeChannel(prev_shape=self.N, next_shape=self.shape))
            self.list_var.extend(
                ['z_0', 'Wz_1', 'Wz_2', 'z_1', 'z_2'])
        
        else : 
            raise NotImplementedError

        return prior_x

    def load_GAN_prior(self, params):
        # load GAN weights
        file = h5py.File(
            f"GAN_VAE_weights/gan_weights/{params['type']}/gan_{params['type']}_{params['id']}.hdf5", "r")
        gen = file["model_weights"]["sequential_1"]
        names = [layer for layer in gen]
        try:
            biases = [gen[layer]["bias:0"][()] for layer in gen]
        except:
            print('no biases')
            biases = []
        weights = [gen[layer]["kernel:0"][()].T for layer in gen]
        shapes = [(gen[layer]["kernel:0"][()].T).shape for layer in gen]
        print(f'GAN weights loaded: {shapes}')
        return names, biases, weights

    def load_VAE_prior(self, params):
        # load GAN weights
        file = h5py.File(
            f"GAN_VAE_weights/vae_weights/{params['type']}/vae_{params['type']}_{params['id']}.h5", "r")
        decoder = file['decoder']

        layers = [decoder[key] for key in list(decoder.keys())]
        weights = [layer["kernel:0"][()].T for layer in layers]
        try:
            biases = [layer["bias:0"][()] for layer in layers]
        except:
            print('no biases')
            biases = []

        shapes = [weight.shape for weight in weights]
        print(f'VAE weights loaded: {shapes}')
        return biases, weights

    def build_model(self, model, y):
        model = model @ GaussianLikelihood(y=y, var=self.Delta)
        model = model.to_model_dag()
        return model

    def run_ep(self, max_iter=250, initializer=None, check_decreasing=True, damping=True, coef_damping=0.5):
        self.max_iter = max_iter
        # Initialization
        initializer = NoisyInit() 

        # Damping variables
        variables_damping = self.build_variable_damping(coef_damping)

        # EP iterations
        ep = ExpectationPropagation(self.model)
        ep.iterate(
            max_iter=max_iter, callback=self.callback, initializer=initializer,
            check_decreasing=check_decreasing, variables_damping=variables_damping)

        ep_x_data = ep.get_variables_data(self.x_ids)
        return ep_x_data

    ### Annex functions ###
    def compute_mse(self, ep_x_data):
        self.x_pred = {x_id: data["r"] for x_id, data in ep_x_data.items()}

        # MSE computed by ep
        self.mse_ep = {x_id: data["v"] for x_id, data in ep_x_data.items()}

        # Real MSE
        self.mse = min( mean_squared_error(self.x_true['x'], self.x_pred['x']), mean_squared_error(self.x_true['x'], -self.x_pred['x']))

        #print(np.min(self.x_pred['x']), np.max(self.x_pred['x']), np.mean(self.x_pred['x']), np.linalg.norm(self.x_pred['x']))
        #print(np.min(self.x_true['x']), np.max(self.x_true['x']), np.mean(self.x_true['x']), np.linalg.norm(self.x_true['x']))

        print(f"mse_ep: {self.mse_ep['x']:.3f} mse: {self.mse: .3f}")
        return self.mse_ep['x'], self.mse

    def build_variable_damping(self, coef_damping=0.5):
        list_var_damping = []
        for var in self.list_var:
            list_var_damping.append((var, "fwd", coef_damping))
            list_var_damping.append((var, "bwd", coef_damping))
        return list_var_damping

    ### Plots ###
    def plot_truth_vs_prediction(self):
        assert self.N == 784
        v_star = self.x_true['x'].reshape(28, 28)
        v_hat = self.x_pred['x'].reshape(28, 28)
        fig, axes = plt.subplots(1, 3)
        axes[0].imshow(v_star, cmap='Greys')

        if self.model_params['name'] == 'inpainting': 
            y_true = self.y_inp.reshape(28, 28)
        elif self.model_params['name'] in ['denoising']: 
            y_true = self.y_true['y'].reshape(28, 28)
        else :
            y_true = v_star

        axes[1].imshow(y_true, cmap='Greys')
        axes[2].imshow(v_hat, cmap='Greys')
        # im = axes[3].imshow(np.abs(v_star-v_hat), cmap='Greys')
        # divider = make_axes_locatable(axes[3])
        # cax = divider.append_axes("right", size="5%", pad=0.05)
        # plt.colorbar(im, cax=cax)
        
        axes[0].set_xlabel(r'$x^\star$')
        axes[1].set_xlabel(r'$x_{\rm obs}$ or $(x^\star)$')
        axes[2].set_xlabel(r'$\hat{x}$')
        #axes[3].set_xlabel(r'$\hat{x}-x^\star$')
        plt.title(f'MSE:{self.mse:.3f}')
        plt.tight_layout()
        # Save
        id = int(time.time())
        file_name = f"./Images/{self.model_params['name']}/{self.data_params['name']}_{self.prior_params['name']}_{self.prior_params['id']}_Delta{self.Delta:.3f}_alpha{self.model_params['alpha']:.3f}_{id}.png"
        plt.savefig(file_name, format='png', dpi=1000,
                    bbox_inches="tight", pad_inches=0.1)
        # Show
        if self.plot_truth_vs_pred:
            plt.show(block=False)
            #input("Press Enter to continue...")
            plt.close()

