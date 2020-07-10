import itertools
import json
import time
from collections import OrderedDict, defaultdict

import networkx as nx
import numpy as np
from rdkit import Chem
from rdchiral.initialization import rdchiralReaction, rdchiralReactants


class MCTS:
    """Monte Carlo Tree Search"""

    def __init__(self, pricer=None, chemhistorian=None, scscorer=None,
                 retro_transformer=None, use_db=False,
                 template_set='reaxys', template_prioritizer='reaxys',
                 precursor_prioritizer='relevanceheuristic',
                 fast_filter='default', **kwargs):

        self.tree = nx.DiGraph()  # directed graph

        self.target = None  # the target compound

        self.chemicals = []  # list of chemical smiles
        self.reactions = []  # list of reaction smiles

        self.iterations = 0
        self.time_to_solve = 0

        # Models and databases
        self.pricer = pricer or self.load_pricer(use_db)
        self.chemhistorian = chemhistorian or self.load_chemhistorian(use_db)
        self.scscorer = scscorer or self.load_scscorer(pricer=self.pricer)

        # If template prioritizer or fast filter are provided, don't load them
        if template_prioritizer is not None and not isinstance(template_prioritizer, str):
            self.template_prioritizer = template_prioritizer
            template_prioritizer = None

        if fast_filter is not None and not isinstance(fast_filter, str):
            self.fast_filter = fast_filter
            fast_filter = None

        self.retro_transformer = retro_transformer or self.load_retro_transformer(
            use_db=use_db,
            template_set=template_set,
            template_prioritizer=template_prioritizer,
            precursor_prioritizer=precursor_prioritizer,
            fast_filter=fast_filter,
        )
        if isinstance(template_prioritizer, str):
            self.template_prioritizer = self.retro_transformer.template_prioritizer
        if isinstance(template_prioritizer, str):
            self.fast_filter = self.retro_transformer.fast_filter
        self.template_set = template_set

        # Retro transformer options
        self.template_max_count = None
        self.template_max_cum_prob = None
        self.fast_filter_threshold = None

        # Tree generation options
        self.expansion_time = None
        self.max_iterations = None
        self.max_chemicals = None
        self.max_reactions = None
        self.max_branching = None
        self.max_depth = None
        self.exploration_weight = None
        self.return_first = None
        self.max_trees = None
        self.banned_chemicals = None
        self.banned_reactions = None

        # Terminal node criteria
        self.max_ppg = None
        self.max_scscore = None
        self.max_elements = None
        self.min_history = None
        self.termination_logic = None
        self.buyables_source = None

        # Parse any keyword arguments and set default options
        self.set_options(**kwargs)

    @property
    def done(self):
        """
        Determine if we're done expanding the tree.
        """
        return (
            self.is_chemical_done(self.target)
            or (self.max_iterations is not None and self.iterations >= self.max_iterations)
            or (self.max_chemicals is not None and len(self.chemicals) >= self.max_chemicals)
            or (self.max_reactions is not None and len(self.reactions) >= self.max_reactions)
        )

    def set_options(self, **kwargs):
        """
        Parse keyword arguments and save options to corresponding attributes.
        Backwards compatible with argument names from original tree builder.

        If no keyword arguments are provided, resets to default options.
        """
        # Retro transformer options
        self.template_max_count = kwargs.get('template_max_count', kwargs.get('template_count', 100))
        self.template_max_cum_prob = kwargs.get('template_max_cum_prob', kwargs.get('max_cum_template_prob', 0.995))
        self.fast_filter_threshold = kwargs.get('fast_filter_threshold', 0.75)

        # Tree generation options
        self.expansion_time = kwargs.get('expansion_time', 30)
        self.max_iterations = kwargs.get('max_iterations', None)
        self.max_chemicals = kwargs.get('max_chemicals', None)
        self.max_reactions = kwargs.get('max_reactions', None)
        self.max_branching = kwargs.get('max_branching', 25)
        self.max_depth = kwargs.get('max_depth', 10)
        self.exploration_weight = kwargs.get('exploration_weight', 1.0)
        self.return_first = kwargs.get('return_first', False)
        self.max_trees = kwargs.get('max_trees', None)
        self.banned_chemicals = kwargs.get('banned_chemicals', kwargs.get('forbidden_molecules', []))
        self.banned_reactions = kwargs.get('banned_reactions', kwargs.get('known_bad_reactions', []))

        # Terminal node criteria
        self.max_ppg = kwargs.get('max_ppg', None)
        self.max_scscore = kwargs.get('max_scscore', None)
        self.max_elements = kwargs.get('max_elements', None)
        self.min_history = kwargs.get('min_history', None)
        self.termination_logic = kwargs.get('termination_logic', {})
        self.buyables_source = kwargs.get('buyables_source', 'all')

    def to_branching(self):
        """
        Get branching representation of the tree.
        """
        branching = nx.dag_to_branching(self.tree)
        # Copy node attributes from original graph
        for node, data in branching.nodes(data=True):
            smiles = data.pop('source')
            data['smiles'] = smiles
            data.update(self.tree.nodes[smiles])
        return branching

    @staticmethod
    def load_chemhistorian(use_db):
        """
        Loads chemhistorian.
        """
        from askcos.utilities.historian.chemicals import ChemHistorian
        chemhistorian = ChemHistorian(use_db=use_db)
        chemhistorian.load()
        return chemhistorian

    @staticmethod
    def load_pricer(use_db):
        """
        Loads pricer.
        """
        from askcos.utilities.buyable.pricer import Pricer
        pricer = Pricer(use_db=use_db)
        pricer.load()
        return pricer

    @staticmethod
    def load_scscorer(pricer=None):
        """
        Loads pricer.
        """
        from askcos.prioritization.precursors.scscore import SCScorePrecursorPrioritizer
        scscorer = SCScorePrecursorPrioritizer(pricer=pricer)
        scscorer.load_model(model_tag='1024bool')
        return scscorer

    @staticmethod
    def load_retro_transformer(use_db, template_set='reaxys', template_prioritizer='reaxys',
                               precursor_prioritizer='relevanceheuristic', fast_filter='default'):
        """
        Loads retro transformer model.
        """
        from askcos.retrosynthetic.transformer import RetroTransformer
        retro_transformer = RetroTransformer(
            use_db=use_db,
            template_set=template_set,
            template_prioritizer=template_prioritizer,
            precursor_prioritizer=precursor_prioritizer,
            fast_filter=fast_filter,
        )
        retro_transformer.load()
        return retro_transformer

    def get_buyable_paths(self, target, **kwargs):
        """
        Build retrosynthesis tree and return paths to buyable precursors.
        """
        self.build_tree(target, **kwargs)
        return self.enumerate_paths(**kwargs)

    def build_tree(self, target, **kwargs):
        """
        Build retrosynthesis tree by iterative expansion of precursor nodes.
        """
        self.set_options(**kwargs)

        print('Initializing tree...')
        self._initialize(target)

        print('Starting tree expansion...')
        start_time = time.time()
        elapsed_time = time.time() - start_time

        while elapsed_time < self.expansion_time and not self.done:
            self._rollout()

            elapsed_time = time.time() - start_time

            self.iterations += 1
            if self.iterations % 100 == 0:
                print('Iteration {0} ({1:.2f}s): |C| = {2} |R| = {3}'.format(self.iterations, elapsed_time, len(self.chemicals), len(self.reactions)))

            if not self.time_to_solve and self.tree.nodes[self.target]['solved']:
                self.time_to_solve = elapsed_time
                print('Found first pathway after {:.2f} seconds.'.format(elapsed_time))
                if self.return_first:
                    print('Stopping expansion to return first pathway.')
                    break

        print('Tree expansion complete.')
        self.print_stats()

    def print_stats(self):
        """
        Print tree statistics.
        """
        info = '\n'
        info += 'Number of iterations: {0}\n'.format(self.iterations)
        num_nodes = self.tree.number_of_nodes()
        info += 'Number of nodes: {0:d}\n'.format(num_nodes)
        info += '    Chemical nodes: {0:d}\n'.format(len(self.chemicals))
        info += '    Reaction nodes: {0:d}\n'.format(len(self.reactions))
        info += 'Number of edges: {0:d}\n'.format(self.tree.number_of_edges())
        if num_nodes > 0:
            info += 'Average in degree: {0:.4f}\n'.format(sum(d for _, d in self.tree.in_degree()) / num_nodes)
            info += 'Average out degree: {0:.4f}'.format(sum(d for _, d in self.tree.out_degree()) / num_nodes)
        print(info)

    def clear(self):
        """
        Clear tree and reset chemicals and reactions.
        """
        self.tree.clear()
        self.chemicals = []
        self.reactions = []

    def dump_tree(self):
        """
        Serialize entire tree to json.
        """
        return json.dumps(nx.node_link_data(self.tree))

    def load_tree(self, data):
        """
        Deserialize and parse tree from json.
        """
        self.tree = nx.node_link_graph(json.loads(data))

    def _initialize(self, target):
        """
        Initialize the tree by with the target chemical.
        """
        self.target = target
        self.create_chemical_node(self.target)
        self.tree.nodes[self.target]['terminal'] = False
        self.tree.nodes[self.target]['done'] = False
        self.tree.nodes[self.target]['solved'] = False

    def _rollout(self):
        """
        Perform one iteration of tree expansion
        """
        chem_path, rxn_path, template = self._select()
        self._expand(chem_path, template)
        self._update(chem_path, rxn_path)

    def _expand(self, chem_path, template):
        """
        Expand the tree by applying chosen template to a chemical node.
        """
        leaf = chem_path[-1]
        explored = self.tree.nodes[leaf]['explored']
        if template not in explored:
            explored.append(template)
            precursors = self._get_precursors(leaf, template)
            self._process_precursors(leaf, template, precursors, chem_path)

    def _update(self, chem_path, rxn_path):
        """
        Update status and reward for nodes in this path.

        Reaction nodes are guaranteed to only have a single parent. Thus, the
        status of its parent chemical will always be updated appropriately in
        ``_update`` and will not change until the next time the chemical is
        in the selected path. Thus, the done state of the chemical can be saved.

        However, chemical nodes can have multiple parents (i.e. can be reached
        via multiple reactions), so a given update cycle may only pass through
        one of multiple parent reactions. Thus, the done state of a reaction
        must be determined dynamically and cannot be saved.
        """
        assert chem_path[0] == self.target, 'Chemical path should start at the root node.'

        # Iterate over the full path in reverse
        # On each iteration, rxn will be the parent reaction of chem
        # For the root (target) node, rxn will be None
        for i, chem, rxn in itertools.zip_longest(range(len(chem_path)-1, -1, -1), reversed(chem_path), reversed(rxn_path)):
            chem_data = self.tree.nodes[chem]
            chem_data['visit_count'] += 1
            chem_data['min_depth'] = min(chem_data['min_depth'], i) if chem_data['min_depth'] is not None else i
            self.is_chemical_done(chem, update=True)
            if rxn is not None:
                rxn_data = self.tree.nodes[rxn]
                rxn_data['visit_count'] += 1
                self._update_value(rxn)

    def is_chemical_done(self, smiles, update=False):
        """
        Determine if the specified chemical node should be expanded further.

        If ``update=True``, will reassess the done state of the node, update
        the ``done`` attribute, and return the new result.

        Otherwise, return the ``done`` node attribute.

        Chemical nodes are done when one of the following is true:
        - The node is terminal
        - The node has exceeded max_depth
        - The node as exceeded max_branching
        - The node does not have any templates to expand
        """
        if update:
            data = self.tree.nodes[smiles]
            done = False
            if data['terminal']:
                done = True
            elif len(data['templates']) == 0:
                done = True
            elif data['min_depth'] is not None and data['min_depth'] >= self.max_depth:
                done = True
            elif self.tree.out_degree(smiles) >= self.max_branching or len(data['explored']) == len(data['templates']):
                done = all(self.is_reaction_done(r) for r in self.tree.successors(smiles))
            data['done'] = done
            return done
        else:
            return self.tree.nodes[smiles]['done']

    def is_reaction_done(self, smiles):
        """
        Determine if the specified reaction node should be expanded further.

        Reaction nodes are done when all of its children chemicals are done.
        """
        return self.tree.out_degree(smiles) > 0 and all(self.is_chemical_done(c) for c in self.tree.successors(smiles))

    def _select(self):
        """
        Select next leaf node to be expanded.

        This starts at the root node (target chemical), and at each level,
        use UCB to score each of the options which can be taken. It will take
        the optimal option, which may be a new template application, or an
        already explored reaction. For the latter, it will descend to the next
        level and repeat the process until a new template application is chosen.
        """
        chem_path = [self.target]
        rxn_path = []
        invalid_options = set()
        template = None
        while template is None:
            leaf = chem_path[-1]
            options = self.ucb(leaf, chem_path, invalid_options, self.exploration_weight)

            if not options:
                # There are no valid options from this chemical node, we need to backtrack
                invalid_options.add(leaf)
                print(invalid_options)
                del chem_path[-1]
                del rxn_path[-1]
                continue

            # Get the best option
            score, task = options[0]

            if isinstance(task, str):
                # This is an already explored reaction, so we need to descend the tree
                # If there are multiple reactants, pick the one with the lower visit count
                # Do not consider chemicals that are already done or chemicals that are on the path
                precursor = min(
                    (c for c in self.tree.successors(task)
                        if not self.is_chemical_done(c)
                        and c not in invalid_options),
                    key=lambda x: self.tree.nodes[x]['visit_count'],
                    default=None,
                )
                if precursor is None:
                    # There are no valid options from this reaction node, we need to backtrack
                    invalid_options.add(task)
                    continue
                else:
                    chem_path.append(precursor)
                    rxn_path.append(task)
            else:
                # This is a new template to apply
                template = task

        return chem_path, rxn_path, template

    def ucb(self, node, path, invalid_options, exploration_weight):
        """
        Calculate UCB score for all exploration options from the specified node.

        This algorithm considers both explored and unexplored template
        applications as potential routes for further exploration.

        Returns a list of (score, option) tuples sorted by score.
        """
        options = []

        templates = self.tree.nodes[node]['templates']
        explored = self.tree.nodes[node]['explored']
        product_visits = self.tree.nodes[node]['visit_count']

        # Get scores for explored templates (reaction node exists)
        for rxn in self.tree.successors(node):
            rxn_data = self.tree.nodes[rxn]

            if self.is_reaction_done(rxn) or len(set(self.tree.successors(rxn)) & set(path)) > 0 or rxn in invalid_options:
                continue

            est_value = rxn_data['est_value']
            node_visits = rxn_data['visit_count']
            template_probability = sum([templates[t] for t in rxn_data['templates']])

            # Q represents how good a move is
            q_sa = template_probability * est_value / node_visits
            # U represents how many times this move has been explored
            u_sa = np.sqrt(np.log(product_visits) / node_visits)

            score = q_sa + exploration_weight * u_sa

            # The options here are to follow a reaction down one level
            options.append((score, rxn))

        # Get score for most relevant unexplored template
        if self.tree.out_degree(node) < self.max_branching or node == self.target:
            for template_index in templates:
                if template_index not in explored:
                    q_sa = templates[template_index]
                    u_sa = np.sqrt(np.log(product_visits))
                    score = q_sa + exploration_weight * u_sa

                    # The options here are to apply a new template to this chemical
                    options.append((score, template_index))
                    break

        # Sort options from highest to lowest score
        options.sort(key=lambda x: x[0], reverse=True)

        return options

    def _get_precursors(self, chemical, template_idx):
        """
        Get all precursors from applying a template to a chemical.
        """
        mol = Chem.MolFromSmiles(chemical)
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        mol = rdchiralReactants(smiles)

        template = self.retro_transformer.get_one_template_by_idx(template_idx)
        try:
            template['rxn'] = rdchiralReaction(template['reaction_smarts'])
        except ValueError:
            return []

        outcomes = self.retro_transformer.apply_one_template(mol, template)

        precursors = [o['smiles_split'] for o in outcomes]

        return precursors

    def _process_precursors(self, target, template, precursors, path):
        """
        Process a list of precursors:
        1. Filter precursors by fast filter score
        2. Create and register Chemical objects for each new precursor
        3. Generate template relevance probabilities
        4. Create and register Reaction objects
        """
        for reactant_list in precursors:
            reactant_smiles = '.'.join(reactant_list)
            reaction_smiles = reactant_smiles + '>>' + target

            # Check if this precursor meets the fast filter score threshold
            ff_score = self.fast_filter(reactant_smiles, target)
            if ff_score < self.fast_filter_threshold:
                continue

            # Check if the reaction is banned
            if reaction_smiles in self.banned_reactions:
                continue

            # Check if any precursors are banned
            if any(reactant in self.banned_chemicals for reactant in reactant_list):
                continue

            for reactant in reactant_list:
                if reactant in self.chemicals:
                    # This is already in the tree somewhere, need to check whether we're creating a cycle
                    if reactant in path or nx.has_path(self.tree, reactant, target):
                        # This would create a cycle
                        break
                else:
                    # This is new, so create a Chemical node
                    self.create_chemical_node(reactant)
            else:
                template_score = self.tree.nodes[target]['templates'][template]

                if reaction_smiles in self.reactions:
                    # This reaction already exists
                    rxn_data = self.tree.nodes[reaction_smiles]
                    rxn_data['templates'].append(template)
                    rxn_data['template_score'] = max(rxn_data['template_score'], template_score)
                else:
                    # This is new, so create a Reaction node
                    self.create_reaction_node(reaction_smiles, template, template_score, ff_score)

                # Add edges to connect target -> reaction -> precursors
                self.tree.add_edge(target, reaction_smiles)
                for reactant in reactant_list:
                    self.tree.add_edge(reaction_smiles, reactant)

                self._update_value(reaction_smiles)

    def create_chemical_node(self, smiles):
        """
        Create a new chemical node from the provide SMILES and populate node
        properties with chemical data.

        Includes template relevance probabilities and purchase price.
        """
        probs, indices = self.template_prioritizer.predict(
            smiles,
            max_num_templates=self.template_max_count,
            max_cum_prob=self.template_max_cum_prob,
        )
        templates = OrderedDict(zip(indices.tolist(), probs.tolist()))

        purchase_price = self.pricer.lookup_smiles(smiles, source=self.buyables_source, alreadyCanonical=True)

        hist = self.chemhistorian.lookup_smiles(smiles, alreadyCanonical=True, template_set=self.template_set)

        terminal = self.is_terminal(smiles, purchase_price, hist)
        est_value = 1. if terminal else 0.

        self.chemicals.append(smiles)
        self.tree.add_node(
            smiles,
            as_reactant=hist['as_reactant'],
            as_product=hist['as_product'],
            est_value=est_value,      # total value of node
            explored=[],              # list of explored templates
            min_depth=None,           # minimum depth at which this chemical appears in the tree
            purchase_price=purchase_price,
            solved=terminal,          # whether a path to terminal leaves has been found from this node
            templates=templates,      # dict of template indices to relevance probabilities
            terminal=terminal,        # whether this chemical meets terminal criterial
            type='chemical',
            visit_count=1,
        )

        self.is_chemical_done(smiles, update=True)

    def create_reaction_node(self, smiles, template, template_score, ff_score):
        """
        Create a new reaction node from the provided smiles and data.
        """
        self.reactions.append(smiles)
        self.tree.add_node(
            smiles,
            est_value=0.,       # score for how feasible a route is, based on whether its precursors are terminal
            ff_score=ff_score,
            solved=False,             # whether a path to terminal leaves has been found from this node
            template_score=template_score,
            templates=[template],
            type='reaction',
            visit_count=1,
        )

    def _update_value(self, smiles):
        """
        Update the value of the specified reaction node and its parent.
        """
        rxn_data = self.tree.nodes[smiles]

        if rxn_data['type'] == 'reaction':
            # Calculate value as the sum of the values of all precursors
            est_value = sum(self.tree.nodes[c]['est_value'] for c in self.tree.successors(smiles))

            # Update estimated value of reaction
            rxn_data['est_value'] += est_value

            # Update estimated value of parent chemical
            chem_data = self.tree.nodes[next(self.tree.predecessors(smiles))]
            chem_data['est_value'] += est_value

            # Check if this node is solved
            solved = rxn_data['solved'] or all(self.tree.nodes[c]['solved'] for c in self.tree.successors(smiles))
            chem_data['solved'] = rxn_data['solved'] = solved

    def is_terminal(self, smiles, ppg=None, hist=None):
        """
        Determine if the specified chemical is a terminal node in the tree based
        on pre-specified criteria.

        The current setup uses ppg as a mandatory criteria, with atom counts and
        chemical history data being optional, additional criteria.

        Args:
            smiles (str): smiles string of the chemical
            ppg (float): cost of the chemical
            hist (dict): historian data for the chemical
        """
        # Default to buyability
        results = {'and': [bool(ppg)], 'or': []}

        if self.max_ppg is not None and ppg is not None:
            logic = self.termination_logic.get('max_ppg', 'and')
            results[logic].append(ppg <= self.max_ppg)

        if self.max_scscore is not None:
            scscore = self.scscorer.get_score_from_smiles(smiles, noprice=True)
            logic = self.termination_logic.get('max_scscore', 'or')
            results[logic].append(scscore <= self.max_scscore)

        if self.max_elements is not None:
            # Get structural properties
            mol = Chem.MolFromSmiles(smiles)
            if mol:
                elem_dict = defaultdict(lambda: 0)
                for a in mol.GetAtoms():
                    elem_dict[a.GetSymbol()] += 1
                elem_dict['H'] = sum(a.GetTotalNumHs() for a in mol.GetAtoms())

                logic = self.termination_logic.get('max_elements', 'or')
                results[logic].append(all(elem_dict[k] <= v for k, v in self.max_elements.items()))

        if self.min_history is not None and hist is not None:
            logic = self.termination_logic.get('min_history', 'or')
            results[logic].append(hist['as_reactant'] >= self.min_history['as_reactant'] or
                                  hist['as_product'] >= self.min_history['as_product'])

        return all(results['and']) or any(results['or'])

    def enumerate_paths(self, path_format='json', sorting_metric='plausibility',
                        validate_paths=True, convert_json=True,
                        max_depth=None, max_trees=None, **kwargs):
        """
        Return list of paths to buyables starting from the target node.
        """
        def _validate_path(_path):
            """Return true if all leaves are terminal."""
            leaves = (v for v, d in _path.out_degree() if d == 0)
            return all(_path.nodes[v]['terminal'] for v in leaves)

        # Resolve template data before doing any node duplication
        self.retrieve_template_data()

        paths = get_paths(
            self.tree, self.target,
            max_depth=max_depth or self.max_depth,
            max_trees=max_trees or self.max_trees,
            validation_fn=_validate_path if validate_paths else None,
        )

        paths = sort_paths(paths, sorting_metric)  # also converts to a list

        print('Found {0} paths to buyable chemicals.'.format(len(paths)))

        if path_format == 'graph':
            pass  # already in graph format
        elif path_format == 'json':
            target = [n for n, s in paths[0].nodes(data='smiles') if s == self.target][0]
            paths = [nx.tree_data(path, target) for path in paths]
            if convert_json:
                paths = [translate_json(path) for path in paths]
        else:
            raise ValueError('Unrecognized format type {0}'.format(path_format))

        return paths

    def retrieve_template_data(self):
        """
        Retrieve template data for all reaction nodes using template ids.
        """
        for rxn in self.reactions:
            rxn_data = self.tree.nodes[rxn]
            template_ids = rxn_data['templates']
            if self.retro_transformer.load_all or not self.retro_transformer.use_db:
                templates = [self.retro_transformer.templates[tid] for tid in template_ids]
            else:
                templates = list(self.retro_transformer.TEMPLATE_DB.find({
                    'index': {'$in': template_ids},
                    'template_set': self.template_set,
                }))
            rxn_data['tforms'] = [str(t.get('_id', -1)) for t in templates]
            rxn_data['num_examples'] = int(sum([t.get('count', 1) for t in templates]))
            rxn_data['necessary_reagent'] = templates[0].get('necessary_reagent', '')


def get_paths(tree, root, max_depth=None, max_trees=None, validation_fn=None):
    """
    Return generator of all paths from the root node as `nx.DiGraph` objects.

    All node attributes are copied to the output paths.
    """
    def get_chem_paths(_node, _uuid, _depth=0):
        """
        Return generator of paths with current node as the root.
        """
        if tree.out_degree(_node) == 0 or max_depth is not None and _depth >= max_depth:
            sub_path = nx.DiGraph()
            sub_path.add_node(_uuid, smiles=_node, **tree.nodes[_node])
            yield sub_path
        else:
            for rxn in tree.successors(_node):
                rxn_uuid = nx.utils.generate_unique_node()
                for sub_path in get_rxn_paths(rxn, rxn_uuid, _depth + 1):
                    sub_path.add_node(_uuid, smiles=_node, **tree.nodes[_node])
                    sub_path.add_edge(_uuid, rxn_uuid)
                    yield sub_path

    def get_rxn_paths(_node, _uuid, _depth=0):
        """
        Return generator of paths with current node as root.
        """
        c_uuid = {c: nx.utils.generate_unique_node() for c in tree.successors(_node)}
        for path_combo in itertools.product(*(get_chem_paths(c, c_uuid[c], _depth) for c in tree.successors(_node))):
            sub_path = nx.union_all(path_combo)
            sub_path.add_node(_uuid, smiles=_node, **tree.nodes[_node])
            for c in tree.successors(_node):
                sub_path.add_edge(_uuid, c_uuid[c])
            yield sub_path

    root_uuid = nx.utils.generate_unique_node()
    num_paths = 0
    for path in get_chem_paths(root, root_uuid):
        if max_trees is not None and num_paths >= max_trees:
            break
        if validation_fn is None or validation_fn(path):
            num_paths += 1
            yield path


def sort_paths(paths, metric):
    """
    Sort paths by some metric.
    """

    def number_of_starting_materials(tree):
        return len([v for v, d in tree.out_degree() if d == 0])

    def number_of_reactions(tree):
        return len([v for v in nx.dag_longest_path(tree) if tree.nodes[v]['type'] == 'reaction'])

    def overall_plausibility(tree):
        return np.prod([d['ff_score'] for v, d in tree.nodes(data=True) if d['type'] == 'reaction'])

    if metric == 'plausibility':
        paths = sorted(paths, key=lambda x: overall_plausibility(x), reverse=True)
    elif metric == 'number_of_starting_materials':
        paths = sorted(paths, key=lambda x: number_of_starting_materials(x))
    elif metric == 'number_of_reactions':
        paths = sorted(paths, key=lambda x: number_of_reactions(x))
    else:
        raise ValueError('Need something to sort by! Invalid option provided: {}'.format(metric))

    return paths


def translate_json(path):
    """
    Convert json output from networkx to match output of old tree builder.

    Input should be a deserialized python object, not a raw json string.
    """
    key_map = {
        'smiles': 'smiles',
        'id': 'id',
        'as_reactant': 'as_reactant',
        'as_product': 'as_product',
        'ff_score': 'plausibility',
        'purchase_price': 'ppg',
        'template_score': 'template_score',
        'tforms': 'tforms',
        'num_examples': 'num_examples',
        'necessary_reagent': 'necessary_reagent',
    }

    output = {}
    for key, value in path.items():
        if key in key_map:
            output[key_map[key]] = value
        elif key == 'type':
            if value == 'chemical':
                output['is_chemical'] = True
            elif value == 'reaction':
                output['is_reaction'] = True
        elif key == 'children':
            output['children'] = [translate_json(c) for c in value]

    if 'children' not in output:
        output['children'] = []

    return output
