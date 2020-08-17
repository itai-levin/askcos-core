import gzip
import json
import os

import pymongo
from bson.objectid import ObjectId
from pymongo import MongoClient
from rdchiral.initialization import rdchiralReaction
from rdkit.Chem import AllChem

import askcos.global_config as gc
from askcos.utilities.io.logger import MyLogger

transformer_loc = 'template_transformer'


class TemplateTransformer(object):
    """One-step retrosynthesis transformer.

    The TemplateTransformer class defines an object which can be used to perform
    one-step chemical tranformations for a given molecule.
    """

    def __init__(self, load_all=gc.PRELOAD_TEMPLATES):
        """Initializes TemplateTransformer.

        Args:
            load_all (bool, optional): Whether to load all of the templates into
                memory. (default: {gc.PRELOAD_TEMPLATES})
        """
        self.templates = []
        self.load_all = load_all
        self.id_to_index = {} # Dictionary to keep track of ID -> index in self.templates

    def doc_to_template(self, document, retro=True):
        """Returns a template given a document from the database or file.

        Args:
            document (dict): Document of template from database or file.

        Returns:
            dict: Retrosynthetic template.
        """
        if 'reaction_smarts' not in document:
            return
        reaction_smarts = str(document['reaction_smarts'])
        if not reaction_smarts:
            return

        if not retro:
            document['rxn_f'] = AllChem.ReactionFromSmarts(reaction_smarts)
            return document

        # different thresholds for chiral and non chiral reactions
        chiral_rxn = False
        for c in reaction_smarts:
            if c in ('@', '/', '\\'):
                chiral_rxn = True
                break

        # Define dictionary
        template = {
            'name':                 document['name'] if 'name' in document else '',
            'reaction_smarts':      reaction_smarts,
            'incompatible_groups':  document['incompatible_groups'] if 'incompatible_groups' in document else [],
            'reference':            document['reference'] if 'reference' in document else '',
            'references':           document['references'] if 'references' in document else [],
            'rxn_example':          document['rxn_example'] if 'rxn_example' in document else '',
            'explicit_H':           document['explicit_H'] if 'explicit_H' in document else False,
            '_id':                  document['_id'] if '_id' in document else -1,
            'product_smiles':       document['product_smiles'] if 'product_smiles' in document else [],
            'necessary_reagent':    document['necessary_reagent'] if 'necessary_reagent' in document else '',
            'efgs':                 document['efgs'] if 'efgs' in document else None,
            'intra_only':           document['intra_only'] if 'intra_only' in document else False,
            'dimer_only':           document['dimer_only'] if 'dimer_only' in document else False,
            'template_set':         document.get('template_set', ''),
            'index':                document.get('index')
        }
        template['chiral'] = chiral_rxn

        # Frequency/popularity score
        template['count'] = document.get('count', 1)

        # Define reaction in RDKit and validate
        try:
            # Force reactants and products to be one pseudo-molecule (bookkeeping)
            reaction_smarts_one = '(' + reaction_smarts.replace('>>', ')>>(') + ')'

            rxn = rdchiralReaction(str(reaction_smarts_one))
            template['rxn'] = rxn

        except Exception as e:
            if gc.DEBUG:
                MyLogger.print_and_log('Couldnt load : {}: {}'.format(
                    reaction_smarts_one, e), transformer_loc, level=1)
            template['rxn'] = None
        return template

    def dump_to_file(self, retro, file_path, chiral=False):
        """Write the template database to a file.

        Args:
            retro (bool): Whether in the retrosynthetic direction.
            file_path (str): Specifies where to save the database.
            chiral (bool, optional): Whether to care about chirality.
                (default: {False})
        """
        if not self.templates:
            raise ValueError('Cannot dump to file if templates have not been loaded')
        
        templates = []

        if retro and chiral:
            # reconstruct template list, but without chiral rxn object
            for template in self.templates:
                templates.append({
                    'name': template['name'],
                    'reaction_smarts': template['reaction_smarts'],
                    'incompatible_groups': template['incompatible_groups'],
                    'references': template['references'],
                    'rxn_example': template['rxn_example'],
                    'explicit_H': template['explicit_H'],
                    '_id': template['_id'],
                    'product_smiles': template['product_smiles'],
                    'necessary_reagent': template['necessary_reagent'],
                    'efgs': template['efgs'],
                    'intra_only': template['intra_only'],
                    'dimer_only': template['dimer_only'],
                    'chiral': template['chiral'],
                    'count': template['count'],
                })
        else:
            templates = self.templates

        if file_path[-2:] != 'gz':
            file_path += '.gz'

        with gzip.open(file_path, 'wb') as f:
            json.dump(templates, f)

        MyLogger.print_and_log('Wrote templates to {}'.format(file_path), transformer_loc)

    def load_from_file(self, file_path, template_set=None, retro=True):
        """Read the template database from a previously saved file.

        Args:
            file_path (str): gzipped json file to read dumped templates from.
            template_set (str): optional name of template set to load, otherwisse load templates from all template sets in file
            retro (bool): whether or not templates being loaded represent retrsynthetic templates
        """

        MyLogger.print_and_log('Loading templates from {}'.format(file_path), transformer_loc)

        if os.path.isfile(file_path):
            with gzip.open(file_path, 'rb') as f:
                self.templates = json.loads(f.read().decode('utf-8'))
        else:
            MyLogger.print_and_log("No file to read data from.", transformer_loc, level=1)
            raise IOError('File not found to load template_transformer from!')

        if template_set is not None and template_set != 'all':
            self.templates = [x for x in self.templates if x.get('template_set') == template_set]
        
        for n, template in enumerate(self.templates):
            if self.load_all:
                template = self.doc_to_template(template, retro=retro)
                self.templates[n] = template
            if template.get('_id') is None:
                template['_id'] = n
            self.id_to_index[template.get('_id')] = n

        self.num_templates = len(self.templates)
        MyLogger.print_and_log('Loaded templates. Using {} templates'.format(self.num_templates), transformer_loc)

    def get_prioritizers(self, *args, **kwargs):
        """Get the prioritization methods for the transformer."""
        raise NotImplementedError

    def load(self, *args, **kwargs):
        """Load and initialize templates."""
        raise NotImplementedError

    def lookup_id(self, template_id):
        """Find the reaction SMARTS for this template_id.

        Args:
            template_id (str, bytes, or ObjectId): ID of requested template.

        Returns:
            Reaction SMARTS for requested template.
        """
        if not self.templates:
            raise ValueError('Cannot lookup template if templates have not been loaded')

        return self.templates[self.id_to_index[template_id]]

    def get_outcomes(self, *args, **kwargs):
        """Gets outcome of single transformation.

        Performs a one-step transformation given a SMILES string of a
        target molecule by applying each transformation template
        sequentially.
        """
        raise NotImplementedError

    def apply_one_template(self, *args, **kwargs):
        """Applies a single template to a given molecule.

        Takes a mol object and applies a single template, returning
        a list of precursors or outcomes, depending on whether retro or
        synthetic templates are used
        """
        raise NotImplementedError
