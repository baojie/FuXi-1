# encoding: utf-8
""
try:
    from itertools import (
        izip as zip,
        ifilter as filter,
        ifilterfalse as filterfalse,
        chain
    )
except ImportError:
    from itertools import chain
import copy
import itertools
import logging
import sys
from pprint import pformat
from rdflib import py3compat, RDF, URIRef, Variable
from rdflib.graph import ConjunctiveGraph, Graph
from rdflib.store import Store
from rdflib.util import first
from rdfextras.sparql.algebra import (
    RenderSPARQLAlgebra,
    NonSymmetricBinaryOperator,
    BasicGraphPattern,
)
# from rdfextras.sparql.query import Query
from rdfextras.store.REGEXMatching import NATIVE_REGEX

# from FuXi.DLP import DisjunctiveNormalForm
from FuXi.Horn.PositiveConditions import BuildUnitermFromTuple
from FuXi.LP.BackwardFixpointProcedure import BackwardFixpointProcedure
from FuXi.LP import IdentifyHybridPredicates
from FuXi.Rete.Magic import (
    ReplaceHybridPredcates,
    SetupDDLAndAdornProgram,
    CreateHybridPredicateRule,
    DerivedPredicateIterator
)
from FuXi.Rete.Network import LOG
from FuXi.Rete.SidewaysInformationPassing import GetOp
from FuXi.Rete.SidewaysInformationPassing import SIPRepresentation
from FuXi.Rete.TopDown import PrepareSipCollection
from FuXi.Rete.TopDown import RDFTuplesToSPARQL
from FuXi.Rete.TopDown import mergeMappings1To2
# from FuXi.Rete.Util import lazyGeneratorPeek
from FuXi.SPARQL import EDBQuery


def _debug(*args, **kw):
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    logger = logging.getLogger(__name__)
    logger.debug(*args, **kw)


__all__ = [
    'BFP_METHOD',
    'DEFAULT_BUILTIN_MAP',
    'RIF_REFERENCE_QUERY',
    'TOP_DOWN_METHOD',
    'TopDownSPARQLEntailingStore',
    ]


TOP_DOWN_METHOD = 0
BFP_METHOD = 1
DEFAULT_BUILTIN_MAP = {
    LOG.equal: "%s = %s", LOG.notEqualTo: "%s != %s"
}

RIF_REFERENCE_QUERY = \
    """
PREFIX  rif: <http://www.w3.org/2007/rif#>
PREFIX ent: <http://www.w3.org/ns/entailment/>
SELECT DISTINCT ?rifUri {
    ?rifUri rif:usedWithProfile ent:Simple
}
"""


class TopDownSPARQLEntailingStore(Store):
    """
    A Store which uses FuXi's magic set "sip strategies" and the in-memory
    SPARQL Algebra implementation as a store-agnostic, top-down decision
    procedure for semanic web SPARQL (OWL2-RL/RIF/N3) entailment regimes.
    Exposed as a rdflib / layercake-python API for SPARQL datasets with
    entailment regimes Queries are mediated over the SPARQL protocol using
    global schemas captured as SW theories which describe and distinguish
    their predicate symbols.
    """
    context_aware = True
    formula_aware = True
    transaction_aware = True
    regex_matching = NATIVE_REGEX
    batch_unification = True

    def getDerivedPredicates(self, expr, prolog):
        if isinstance(expr, BasicGraphPattern):
            for s, p, o, func in expr.patterns:
                derivedPred = self.derivedPredicateFromTriple((s, p, o))
                if derivedPred is not None:
                    yield derivedPred
        elif isinstance(expr, NonSymmetricBinaryOperator):
            for term in self.getDerivedPredicates(expr.left, prolog):
                yield term
            for term in self.getDerivedPredicates(expr.right, prolog):
                yield term
        else:
            raise NotImplementedError(expr)

    @py3compat.format_doctest_out
    def isaBaseQuery(self, queryString, queryObj=None):
        """
        If the given SPARQL query involves purely base predicates
        it returns it (as a parsed string), otherwise it returns a SPARQL
        algebra instance for top-down evaluation using this store

        >>> graph = Graph()  # doctest: +SKIP
        >>> topDownStore = TopDownSPARQLEntailingStore( # doctest: +SKIP
        ...    graph.store, [RDFS.seeAlso], nsBindings={ # doctest: +SKIP
        ...         %(u)s'rdfs':RDFS}) # doctest: +SKIP
        >>> rt=topDownStore.isaBaseQuery(  # doctest: +SKIP
        ...    "SELECT * { [] rdfs:seeAlso [] }") # doctest: +SKIP
        >>> isinstance(rt,  # doctest: +SKIP
        ...    (BasicGraphPattern, AlgebraExpression)) # doctest: +SKIP
        True
        >>> rt=topDownStore.isaBaseQuery( # doctest: +SKIP
        ...    "SELECT * { [] a [] }") # doctest: +SKIP
        >>> isinstance(rt,(Query,basestring)) # doctest: +SKIP
        True
        >>> rt=topDownStore.isaBaseQuery( # doctest: +SKIP
        ...    "SELECT * {[] a [] OPTIONAL {[] rdfs:seeAlso []}}") # doctest: +SKIP
        >>> isinstance(rt,(BasicGraphPattern,AlgebraExpression)) # doctest: +SKIP
        True
        """
        from rdfextras.sparql.components import Prolog
        from rdfextras.sparql.parser import parse
        from rdfextras import sparql as sparqlModule
        if queryObj:
            query = queryObj
        else:
            query = parse(queryString)
        if not query.prolog:
                query.prolog = Prolog(None, [])
                query.prolog.prefixBindings.update(self.nsBindings)
        else:
            for prefix, nsInst in list(self.nsBindings.items()):
                if prefix not in query.prolog.prefixBindings:
                    query.prolog.prefixBindings[prefix] = nsInst

        sparqlModule.prolog = query.prolog
        algebra = RenderSPARQLAlgebra(query, nsMappings=self.nsBindings)
        return first(self.getDerivedPredicates(
            algebra, sparqlModule.prolog)) and algebra or query

    def __init__(self,
                 store,
                 edb,
                 derivedPredicates=None,
                 idb=None,
                 DEBUG=False,
                 nsBindings={},
                 decisionProcedure=BFP_METHOD,
                 templateMap=None,
                 identifyHybridPredicates=False,
                 hybridPredicates=None):
        self.dataset = store
        hybridPredicates = hybridPredicates if hybridPredicates else []
        if hasattr(store, '_db'):
            self._db = store._db
        self.idb = idb if idb else set()
        self.edb = edb
        for rifUri in edb.query(RIF_REFERENCE_QUERY):
            try:
                from FuXi.Horn.RIFCore import RIFCoreParser
                _debug("rifUri: %s" % rifUri)
                if rifUri in [i.identifier
                        for i in ConjunctiveGraph(edb.store).contexts()]:
                    if DEBUG:
                        _debug("RIF in RDF is in named graph %s" % rifUri.n3())
                    rif_parser = RIFCoreParser(
                        graph=Graph(edb.store, rifUri), debug=DEBUG)
                else:
                    if DEBUG:
                        _debug("RIF / XML is remote")
                    rif_parser = RIFCoreParser(location=rifUri, debug=DEBUG)
                self.idb.update(rif_parser.getRuleset())
            except ImportError:
                raise Exception(
                    "Missing 3rd party libraries for RIF processing:"
                )
            if DEBUG:
                _debug(pformat(list(self.idb)))
        if derivedPredicates is None:
            self.derivedPredicates = list(
                DerivedPredicateIterator(self.edb, self.idb))
        else:
            self.derivedPredicates = derivedPredicates
        self.DEBUG = DEBUG
        self.nsBindings = nsBindings
        self.edb.templateMap = DEFAULT_BUILTIN_MAP if templateMap is None\
            else templateMap
        self.queryNetworks = []
        self.edbQueries = set()
        if identifyHybridPredicates:
            self.hybridPredicates = IdentifyHybridPredicates(
                        edb, self.derivedPredicates)
        else:
            self.hybridPredicates = hybridPredicates if hybridPredicates else [
            ]

        # Update derived predicate list for synchrony with hybrid predicate
        # rules
        if self.hybridPredicates and self.idb:
            d_predicates, new_rules = self.hybridPredicatePreparation(
                self.nsBindings
            )
            self.idb = new_rules
            self.derivedPredicates = d_predicates

        # Add a cache of the namespace bindings to use later in coining Qnames
        # in generated queries
        self.edb.revNsMap = {}
        self.edb.nsMap = {}
        for k, v in list(nsBindings.items()):
            self.edb.revNsMap[v] = k
            self.edb.nsMap[k] = v
        for key, uri in self.edb.namespaces():
            self.edb.revNsMap[uri] = key
            self.edb.nsMap[key] = uri

    def invokeDecisionProcedure(self,
                                tp,
                                factGraph,
                                bindings,
                                debug,
                                sipCollection):
        from FuXi.Rete.RuleStore import SetupRuleStore
        isNotGround = first(
                        filter(
                            lambda i: isinstance(i, Variable), tp))
        rule_store, rule_graph, network = SetupRuleStore(makeNetwork=True)
        bfp = BackwardFixpointProcedure(
            factGraph,
            network,
            self.derivedPredicates,
            tp,
            sipCollection,
            hybridPredicates=self.hybridPredicates,
            debug=self.DEBUG)
        bfp.createTopDownReteNetwork(self.DEBUG)
        if self.DEBUG:
            _debug("Goal/Query: %s " % tp)
            _debug("Query was not ground" \
                    if isNotGround is not None \
                    else "Query was ground")
        rt = bfp.answers(debug=self.DEBUG)
        self.queryNetworks.append((bfp.metaInterpNetwork, tp))
        self.edbQueries.update(bfp.edbQueries)
        if isNotGround is not None:
            for item in bfp.goalSolutions:
                yield item, None
        else:
            yield rt, None
        if debug:
            _debug(bfp.metaInterpNetwork)
            bfp.metaInterpNetwork.reportConflictSet(True, sys.stderr)
            for query in self.edbQueries:
                _debug(
                    "Dispatched query against dataset: %s" % query.asSPARQL())

    def hybridPredQueryPreparation(self, tp):
        lit = BuildUnitermFromTuple(tp)
        op = GetOp(lit)
        if op in self.hybridPredicates:
            lit.setOperator(URIRef(op + '_derived'))
            return lit.toRDFTuple()
        else:
            return tp

    def hybridPredicatePreparation(self, nsMapping=None):
        from FuXi.Horn.HornRules import Ruleset
        new_rules = list(map(copy.deepcopy, self.idb))
        ReplaceHybridPredcates(new_rules, self.hybridPredicates)
        for hybrid_predicate in self.hybridPredicates:
            new_rules.append(
                CreateHybridPredicateRule(
                    hybrid_predicate,
                    self.idb,
                    self.nsBindings
                )
            )
        new_derived_predicates = set([i for i in self.derivedPredicates
                                      if i not in self.hybridPredicates])
        new_derived_predicates.update(
            [URIRef(i + '_derived') for i in self.hybridPredicates]
        )
        new_derived_predicates = list(new_derived_predicates)
        if self.DEBUG:
            _debug("Hybrid predicates: %s" % self.hybridPredicates)
            _debug("Updated to account for hybrid predicates")
            _debug(new_rules)
            _debug("Derived predicates")
            _debug(pformat(new_derived_predicates))
        return new_derived_predicates, Ruleset(
            formulae=new_rules,
            nsMapping=nsMapping)

    def conjunctiveSipStrategy(self, goalsRemaining, factGraph, bindings=None):
        """
        Given a conjunctive set of triples, invoke sip-strategy passing
        on intermediate solutions to facilitate 'join' behavior
        """
        bindings = bindings if bindings else {}
        try:
            tp = next(goalsRemaining)
            assert isinstance(bindings, dict)
            dPred = self.derivedPredicateFromTriple(tp)
            if dPred is None:
                baseEDBQuery = EDBQuery([BuildUnitermFromTuple(tp)],
                                        self.edb,
                                        bindings=bindings)
                if self.DEBUG:
                    _debug(
                        "Evaluating TP against EDB: %s" %
                            baseEDBQuery.asSPARQL())
                query, rt = baseEDBQuery.evaluate()
                if isinstance(rt, bool) and rt:
                    yield bindings
                elif not isinstance(rt, bool):
                    rt = list(rt)
                    remaining_goals = itertools.tee(goalsRemaining, len(rt))
                    for idx in range(len(rt)):
                        item = {}
                        item.update(rt[idx])
                        item.update(bindings)
                        if self.DEBUG:
                            _debug("Solution from EDB query: %s" % item)
                        for ansDict in self.conjunctiveSipStrategy(
                            remaining_goals[idx],
                            factGraph,
                                item):
                            yield ansDict

            else:
                queryLit = BuildUnitermFromTuple(tp)
                currentOp = GetOp(queryLit)
                queryLit.setOperator(currentOp)
                query = EDBQuery([queryLit],
                                 self.edb,
                                 bindings=bindings)
                if bindings:
                    tp = first(query.formulae).toRDFTuple()
                if self.DEBUG:
                    _debug("Goal/Query: %s" % query.asSPARQL())
                tp = self.hybridPredQueryPreparation(tp)
                SetupDDLAndAdornProgram(
                    self.edb,
                    self.idb,
                    [tp],
                    derivedPreds=self.derivedPredicates,
                    ignoreUnboundDPreds=True)

                sipCollection = PrepareSipCollection(self.edb.adornedProgram)
                if self.DEBUG and sipCollection:
                    for sip in SIPRepresentation(sipCollection):
                        _debug(sip)
                    _debug(pformat(list(self.edb.adornedProgram)))
                elif self.DEBUG:
                    _debug("No SIP graph.")
                for nextAnswer, ns in self.invokeDecisionProcedure(
                    tp,
                    factGraph,
                    bindings,
                    self.DEBUG,
                        sipCollection):
                    if isinstance(nextAnswer, dict):
                        # Received solutions to 'open' query, merge with given
                        # bindings and continue
                        for ansDict in self.conjunctiveSipStrategy(
                            goalsRemaining,
                            factGraph,
                            mergeMappings1To2(bindings,
                                              nextAnswer)):
                            yield ansDict
                    elif nextAnswer:
                        # we (successfully) proved a ground query, pass on
                        # bindings
                        assert isinstance(nextAnswer, bool)
                        for ansDict in self.conjunctiveSipStrategy(
                            goalsRemaining,
                            factGraph,
                                bindings):
                            yield ansDict
        except StopIteration:
            yield bindings

    def derivedPredicateFromTriple(self, triple):
        """
        Given a triple, return its predicate (if derived)
        or None otherwise
        """
        (s, p, o) = triple
        if p in self.derivedPredicates or p in self.hybridPredicates:
            return p
        elif p == RDF.type and o != p and (
            o in self.derivedPredicates or
                o in self.hybridPredicates):
            return o
        else:
            return None

    def sparql_query(self,
                     queryString,
                     queryObj,
                     graph,
                     dataSetBase,
                     extensionFunctions,
                     initBindings={},
                     initNs={},
                     DEBUG=False):
        """
        The default 'native' SPARQL implementation is based on sparql-p's
        expansion trees layered on top of the read-only RDF APIs of the
        underlying store.
        """
        from rdfextras.sparql.algebra import TopEvaluate
        from rdfextras.query import SPARQLQueryResult as QueryResult
        from rdflib import plugin
        from rdfextras.sparql.components import AskQuery
        _expr = self.isaBaseQuery(None, queryObj)
        if isinstance(queryObj.query, AskQuery) and \
                isinstance(_expr, BasicGraphPattern):
            # This is a ground, BGP, involving IDB and can be solved directly
            # using top-down decision procedure
            # First separate out conjunct into EDB and IDB predicates
            # (solving the former first)
            from FuXi.SPARQL import EDBQuery
            groundConjunct = []
            derivedConjunct = []
            for s, p, o, func in _expr.patterns:
                if self.derivedPredicateFromTriple((s, p, o)) is None:
                    groundConjunct.append(BuildUnitermFromTuple((s, p, o)))
                else:
                    derivedConjunct.append(BuildUnitermFromTuple((s, p, o)))
            if groundConjunct:
                baseEDBQuery = EDBQuery(groundConjunct, self.edb)
                subQuery, ans = baseEDBQuery.evaluate(DEBUG)
                assert isinstance(ans, bool), ans
            if groundConjunct and not ans:
                askResult = False
            else:
                askResult = True
                for derivedLiteral in derivedConjunct:
                    goal = derivedLiteral.toRDFTuple()
                    # Solve ground, derived goal directly

                    goal = self.hybridPredQueryPreparation(goal)

                    SetupDDLAndAdornProgram(
                        self.edb,
                        self.idb,
                        [goal],
                        derivedPreds=self.derivedPredicates,
                        ignoreUnboundDPreds=True)

                    sipCollection = PrepareSipCollection(
                        self.edb.adornedProgram)
                    if self.DEBUG and sipCollection:
                        for sip in SIPRepresentation(sipCollection):
                            _debug(sip)
                        _debug(pformat(list(self.edb.adornedProgram)))
                    elif self.DEBUG:
                        _debug("No SIP graph.")

                    rt, node = first(self.invokeDecisionProcedure(
                        goal,
                        self.edb,
                        {},
                        self.DEBUG,
                        sipCollection))
                    if not rt:
                        askResult = False
                        break
            return plugin.get('SPARQLQueryResult', QueryResult)(askResult)
        else:
            rt = TopEvaluate(queryObj,
                             graph,
                             initBindings,
                             DEBUG=self.DEBUG,
                             dataSetBase=dataSetBase,
                             extensionFunctions=extensionFunctions)
            return plugin.get('SPARQLQueryResult', QueryResult)(rt)

    def batch_unify(self, patterns):
        """
        Perform RDF triple store-level unification of a list of triple
        patterns (4-item tuples which correspond to a SPARQL triple pattern
        with an additional constraint for the graph name).

        Uses a SW sip-strategy implementation to solve the conjunctive goal
        and yield unified bindings

        :Parameters:
        - `patterns`: a list of 4-item tuples where any of the items can be
           one of: Variable, URIRef, BNode, or Literal.

        Returns a generator over dictionaries of solutions to the list of
        triple patterns that are entailed by the regime.
        """
        dPreds = set()
        goals = []
        for s, p, o, g in patterns:
            goals.append((s, p, o))
            dPred = o if p == RDF.type else p
            if dPred in self.hybridPredicates:
                dPreds.add(URIRef(dPred + '_derived'))
            else:
                dPreds.add(p == RDF.type and o or p)
        if set(dPreds).intersection(self.derivedPredicates):
            #Patterns involve derived predicates
            self.batch_unification = False
            for ansDict in self.conjunctiveSipStrategy(
                iter(goals),
                    self.edb):
                yield ansDict
            self.batch_unification = True
        else:
            #conjunctive query involving EDB predicateso only
            vars = []
            triples = []
            for pat in patterns:
                triples.append(BuildUnitermFromTuple(pat[:3]))
                vars.extend([term for term in pat[:3]
                             if isinstance(term, Variable)])

            vars = list(set(vars))
            query = RDFTuplesToSPARQL(triples, self.edb, vars=vars)
            graphNsMap = dict(self.edb.namespaces())
            graphNsMap.update(self.nsBindings)
            prefixDefs = '\n'.join([
                "PREFIX %s: %s" % (
                    k, v if isinstance(v, basestring) else v.n3())
                for k, v in list(graphNsMap.items()) if k])
            baseDef = ''  # "BASE %s" %
                          # graphNsMap.get(u'').n3() \
                          # if u'' in graphNsMap else u''
            query = '\n'.join([baseDef, prefixDefs, query])
            if self.DEBUG:
                _debug("Batch unify resolved against EDB")
                _debug(query)
            _debug("Batch unify resolved against EDB")
            _debug(query)
            rt = self.edb.query(query, initNs=self.nsBindings)
            rt = len(vars) > 1 and (dict([(vars[idx], i)
                                          for idx, i in enumerate(v)])
                                    for v in rt) \
                or (dict([(vars[0], v)]) for v in rt)
            for item in rt:
                yield item

    def close(self, commit_pending_transaction=False):
        """
        This closes the database connection. The commit_pending_transaction
        parameter specifies whether to commit all pending transactions before
        closing (if the store is transactional).
        """
        return self.dataset.close(commit_pending_transaction)

    def destroy(self, configuration):
        """
        This destroys the instance of the store identified by the configuration
        string.
        """
        return self.dataset.destroy(configuration)

    def triples_choices(self, triple, context=None):
        """
        A variant of triples that can take a list of terms instead of a single
        term in any slot.  Stores can implement this to optimize the response
        time from the default 'fallback' implementation, which will iterate
        over each term in the list and dispatch to tripless
        """
        (subject, predicate, object_) = triple
        for rt in self.dataset.triples_choices(
                (subject, predicate, object_), context):
            yield rt

    def triples(self, triple, context=None):
        """
        A generator over all the triples matching the pattern. Pattern can
        include any objects for used for comparing against nodes in the store,
        for example, REGEXTerm, URIRef, Literal, BNode, Variable, Graph,
        QuotedGraph, Date? DateRange?

        A conjunctive query can be indicated by either providing a value of
        None for the context or the identifier associated with the Conjunctive
        Graph (if it's context aware).
        """
        (subject, predicate, object) = triple
        for rt in self.dataset.triples((subject, predicate, object), context):
            yield rt

    def __len__(self, context=None):
        """
        Number of statements in the store. This should only account for non-
        quoted (asserted) statements if the context is not specified,
        otherwise it should return the number of statements in the formula or
        context given.
        """
        return len(self.dataset)

    def contexts(self, triple=None):
        """
        Generator over all contexts in the graph. If triple is specified, a
        generator over all contexts the triple is in.
        """
        for ctx in self.dataset.contexts(triple):
            yield ctx

    # Optional Namespace methods

    def bind(self, prefix, namespace):
        self.nsBindings[prefix] = namespace
        #self.targetGraph.bind(prefix,namespace)

    def prefix(self, namespace):
        revDict = dict([(v, k) for k, v in list(self.nsBindings.items())])
        return revDict.get(namespace)

    def namespace(self, prefix):
        return self.nsBindings.get(prefix)

    def namespaces(self):
        for prefix, nsUri in list(self.nsBindings.items()):
            yield prefix, nsUri

    # Optional Transactional methods

    def commit(self):
        self.dataset.commit()

    def rollback(self):
        self.dataset.rollback()


def test():
    import doctest
    doctest.testmod()

if __name__ == '__main__':
    test()

# from FuXi.SPARQL.BackwardChainingStore import TOP_DOWN_METHOD
# from FuXi.SPARQL.BackwardChainingStore import BFP_METHOD
# from FuXi.SPARQL.BackwardChainingStore import DEFAULT_BUILTIN_MAP
# from FuXi.SPARQL.BackwardChainingStore import RIF_REFERENCE_QUERY
# from FuXi.SPARQL.BackwardChainingStore import TopDownSPARQLEntailingStore
