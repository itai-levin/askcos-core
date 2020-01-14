import makeit.global_config as gc
from makeit.prioritization.prioritizer import Prioritizer
import rdkit.Chem as Chem
from rdkit.Chem import AllChem
import numpy as np
from makeit.utilities.io.logger import MyLogger
import tensorflow as tf
from scipy.special import softmax
import requests

relevance_template_prioritizer_loc = 'relevance_template_prioritizer'


class RelevanceTemplatePrioritizer(Prioritizer):
    """A template Prioritizer based on template relevance.

    Attributes:
        fp_length (int): Fingerprint length.
        fp_radius (int): Fingerprint radius.
    """

    def __init__(self, fp_length=2048, fp_radius=2):
        self.fp_length = fp_length
        self.fp_radius = fp_radius

    def load_model(self, model_path=gc.RELEVANCE_TEMPLATE_PRIORITIZATION['reaxys']['model_path'], **kwargs):
        """Loads a model to predict template priority.

        Args:
            model_path (str): Path to keras saved model to be loaded with
                tf.keras.models.load_model. **kwargs are passed to load_model
                to allow loading of custom_objects

        """
        self.model = tf.keras.models.load_model(model_path, **kwargs)

    def smiles_to_fp(self, smiles):
        """Converts SMILES string to fingerprint for use with template relevance model.

        Args:
            smiles (str): SMILES string to convert to fingerprint

        Returns:
            np.ndarray of np.float32: Fingerprint for given SMILES string.

        """
        mol = Chem.MolFromSmiles(smiles)
        if not mol:
            return np.zeros((self.fp_length,), dtype=np.float32)
        return np.array(
            AllChem.GetMorganFingerprintAsBitVect(
                mol, self.fp_radius, nBits=self.fp_length, useChirality=True
            ), dtype=np.float32
        )

    def predict(self, smiles, max_num_templates, max_cum_prob):
        """Predicts template priority given a SMILES string.

        Args:
            smiles (str): SMILES string of input molecule
            max_num_templates (int): Maximum number of template scores
                and indices to return
            max_cum_prob (float): Maximum cumulative probability of template
                scores to return. Scores and indices will be returned up until
                max_cum_prob is exceeded.

        Returns:
            (scores, indices): np.ndarrays of scores and indices for 
                prioritized templates
        """
        fp = self.smiles_to_fp(smiles)
        scores = self.model.predict(fp.reshape(1, -1)).reshape(-1)
        indices = np.argsort(-scores)[:max_num_templates]
        scores = softmax(scores[indices])
        truncate = np.argmax(np.cumsum(scores)>max_cum_prob)
        return scores[:truncate], indices[:truncate]




class TemplateRelevanceTFServingAPI(RelevanceTemplatePrioritizer):
    """Template relevance prioritization model served using TF serving.
    
    Attributes:
        hostname (str): hostname of service serving tf model.
        model_name (str): Name of model provided to tf serving.
        fp_length (int): Fingerprint length.
        fp_radius (int): Fingerprint radius.

    """
    def __init__(self, hostname, model_name, fp_length=2048, fp_radius=2):
        self.fp_length = fp_length
        self.fp_radius = fp_radius
        self.url = 'http://{}:8501/v1/models/{}:predict'.format(
            hostname, model_name
        )

    def load(self, model_path=None):
        """Override load method, no model to load"""
        pass

    def predict(self, smiles, max_num_templates, max_cum_prob):
        """Makes template relevance prediction using TF Serving API.

        Args:
            smiles (str): SMILES string of input molecule
            max_num_templates (int): Maximum number of template scores
                and indices to return
            max_cum_prob (float): Maximum cumulative probability of template
                scores to return. Scores and indices will be returned up until
                max_cum_prob is exceeded.

        Returns:
            (scores, indices): np.ndarrays of scores and indices for 
                prioritized templates
        """
        fp = self.smiles_to_fp(smiles).reshape(1, -1).tolist()
        resp = requests.post(self.url, json={'instances': fp})
        scores = np.array(resp.json()['predictions']).reshape(-1)
        indices = np.argsort(-scores)[:max_num_templates]
        scores = softmax(scores[indices])
        truncate = np.argmax(np.cumsum(scores)>max_cum_prob)
        return scores[:truncate], indices[:truncate]


if __name__ == '__main__':
    model = RelevanceTemplatePrioritizer()
    model.load_model()
    smis = ['CCCOCCC', 'CCCNc1ccccc1']
    for smi in smis:
        lst = model.predict(smi)
        print('{} -> {}'.format(smi, lst))
