#!/usr/bin/env python
# encoding: utf-8
"""
Implementation of Sideways Information Passing graph (builds it from a given
ruleset)
"""

import itertools
from hashlib import md5
from FuXi.Horn.PositiveConditions import (
    And,
    Exists,
    SetOperator,
    Uniterm
)
from FuXi.Rete.RuleStore import N3Builtin
from FuXi.DLP import SKOLEMIZED_CLASS_NS
from FuXi.DLP.Negation import ProperSipOrderWithNegation
from rdflib.collection import Collection
from rdflib.graph import Graph
from rdflib import BNode, Namespace, RDF, URIRef, Variable
from rdflib.util import first
from functools import reduce

# required for doctests to pass
from FuXi.Rete.RuleStore import SetupRuleStore
from FuXi.Horn.HornRules import Ruleset
from io import StringIO
from pprint import pprint


__all__ = [
    'BuildNaturalSIP',
    'CollectSIPArcVars',
    'findFullSip',
    'GetArgs',
    'getOccurrenceId',
    'GetOp',
    'GetVariables',
    'IncomingSIPArcs',
    'InvalidSIPException',
    'iterCondition',
    'makeMD5Digest',
    'normalizeTerm',
    'RenderSIPCollection',
    'SetOp',
    'SIPGraphArc',
    'SIPRepresentation',
    'validSip',
]


def _debug(*args, **kw):
    import logging
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    logger = logging.getLogger(__name__)
    logger.debug(*args, **kw)


def makeMD5Digest(value):
    return md5(
        isinstance(value, str) and value.encode('utf-8')
        or value).hexdigest()

MAGIC = Namespace('http://doi.acm.org/10.1145/28659.28689#')


def iterCondition(condition):
    if isinstance(condition, Exists):
        return iterCondition(condition.formula)
    else:
        return isinstance(condition, SetOperator) \
            and condition or iter([condition])


def normalizeTerm(uri, sipGraph):
    try:
        return sipGraph.qname(uri).split(':')[-1]
    except:
        return uri.n3()


def RenderSIPCollection(sipGraph, dot=None):
    try:
        from pydot import Node, Edge, Dot
    except:
        import warnings
        warnings.warn("Missing pydot library", ImportWarning)
    if not dot:
        dot = Dot(graph_type='digraph')
        dot.leftNodesLookup = {}
    nodes = {}
    for N, prop, q in sipGraph.query(
        'SELECT ?N ?prop ?q {  ?prop a magic:SipArc . ?N ?prop ?q . }',
            initNs={u'magic': MAGIC}):

        if MAGIC.BoundHeadPredicate in sipGraph.objects(subject=N,
                                                        predicate=RDF.type):
            NCol = [N]
        else:
            NCol = Collection(sipGraph, N)

        if q not in nodes:
            newNode = Node(makeMD5Digest(q),
                           label=normalizeTerm(q, sipGraph),
                           shape='plaintext')
            nodes[q] = newNode
            dot.add_node(newNode)

        bNode = BNode()
        nodeLabel = ', '.join([normalizeTerm(term, sipGraph)
                               for term in NCol])
        edgeLabel = ', '.join(
            [var.n3() for var in Collection(
             sipGraph, first(sipGraph.objects(prop, MAGIC.bindings)))])
        markedEdgeLabel = ''
        if nodeLabel in dot.leftNodesLookup:
            bNode, leftNode, markedEdgeLabel = dot.leftNodesLookup[nodeLabel]
            # _debug("\t%s %s %s %s", nodeLabel, edgeLabel,
            #           markedEdgeLabel, not edgeLabel == markedEdgeLabel)
        else:
            leftNode = Node(makeMD5Digest(bNode),
                            label=nodeLabel, shape='plaintext')
            dot.leftNodesLookup[nodeLabel] = (bNode, leftNode, edgeLabel)
            nodes[bNode] = leftNode
            dot.add_node(leftNode)

        if not edgeLabel == markedEdgeLabel:
            edge = Edge(leftNode,
                        nodes[q],
                        label=edgeLabel)
            dot.add_edge(edge)
    return dot


class SIPGraphArc(object):
    """
    A `sip` for `r` is a labeled graph that satisfies the following conditions:
    1. Each node is either a subset or a member of `P(r)` or `{ph}`.
    2. Each arc is of the form `N -> q`, with label `X`, where `N` is a subset
    of `P(r)` or `{ph}`, `q` is a member of `P(r)` and `X` is a set of
    variables such that
    (i) Each variable of `X` appears in `N`.
    (ii) Each member of `N` is connected to a variable in `X`.
    (iii) For some argument of `q`, all its variables appear in `X`. Further,
    each variable of `X` appears in an argument of `q` that satisfies this
    condition.
    """
    def __init__(self, left, right, variables, graph=None, headPassing=False):
        self.variables = variables
        self.left = left
        self.right = right
        self.graph = graph is None and Graph() or graph
        self.arc = SKOLEMIZED_CLASS_NS[BNode()]
        self.graph.add((self.arc, RDF.type, MAGIC.SipArc))
        varsCol = Collection(self.graph, BNode())
        [varsCol.append(i) for i in self.variables]
        self.graph.add((self.arc, MAGIC.bindings, varsCol.uri))
        if headPassing:
            self.boundHeadPredicate = True
            self.graph.add((self.left, self.arc, self.right))
        else:
            self.boundHeadPredicate = False
            self.graph.add((self.left, self.arc, self.right))

    def __repr__(self):
        """Visual of graph arc"""
        return "%s - (%s) > %s" % (
            self.left, self.variables, self.right)


def CollectSIPArcVars(left, right, phBoundVars):
    """docstring for CollectSIPArcVars"""
    if isinstance(left, list):
        return set(reduce(
            lambda x, y: x + y,
            [hasattr(t, 'isHead') and phBoundVars
             or GetArgs(t, secondOrder=True)
             for t in left])
        ).intersection(GetArgs(right, secondOrder=True))
    else:
        incomingVarsToInclude = phBoundVars and phBoundVars or \
            GetArgs(left, secondOrder=True)
        return set(incomingVarsToInclude
                   ).intersection(GetArgs(right, secondOrder=True))


def SetOp(term, value):
    """docstring for SetOp"""
    if isinstance(term, N3Builtin):
        term.uri = value
    elif isinstance(term, Uniterm):
        if term.op == RDF.type:
            term.arg[-1] = value
        else:
            term.op = value
    else:
        raise Exception('''SetOp unable to set op of "%s"''' % term)


def GetOp(term):
    """docstring for GetOp"""
    if isinstance(term, N3Builtin):
        return term.uri
    elif isinstance(term, Uniterm):
        return term.op == RDF.type and term.arg[-1] or term.op
    elif isinstance(term, Exists):
        return GetOp(term.formula)
    else:
        raise Exception('''GetOp unable to get op of "%s"''' % term)


def GetVariables(term, secondOrder=False):
    """docstring for GetVariables"""
    for v in GetArgs(term, secondOrder):
        if isinstance(v, Variable):
            yield v


def GetArgs(term, secondOrder=False):
    """docstring for GetArgs"""
    if isinstance(term, N3Builtin):
        return [term.argument, term.result]
    elif isinstance(term, Uniterm):
        args = []
        if term.op == RDF.type:
            if secondOrder and isinstance(term.arg[-1], (Variable, BNode)):
                args.extend(term.arg)
            else:
                args.append(term.arg[0])
        elif isinstance(term.op, (Variable, BNode)):
            args.append(term.op)
            args.extend(term.arg)
        else:
            args.extend(term.arg)
        return args
    elif isinstance(term, Exists):
        return GetArgs(term.formula, secondOrder)
    else:
        raise term


def IncomingSIPArcs(sip, predOcc):
    """docstring for IncomingSIPArcs"""
    for s, p, o in sip.triples((None, None, predOcc)):
        if (p, RDF.type, MAGIC.SipArc) in sip:
            if (s, RDF.type, MAGIC.BoundHeadPredicate) in sip:
                yield [s], Collection(
                    sip, first(sip.objects(p, MAGIC.bindings)))
            else:
                yield Collection(sip, s), Collection(
                    sip, first(sip.objects(p, MAGIC.bindings)))


def validSip(sipGraph):
    """docstring for validSip"""
    if not len(sipGraph):
        return False
    for arc in sipGraph.query(
        "SELECT ?arc { ?arc m:bindings ?bindings OPTIONAL" +
        "{ ?bindings rdf:first ?val } FILTER(!BOUND(?val)) }",
            initNs={'m': MAGIC}):
        return False
    return True


def getOccurrenceId(uniterm, lookup={}):
    """docstring for SetOp"""
    pO = URIRef(GetOp(uniterm) + '_' + '_'.join(GetArgs(uniterm)))
    lookup[pO] = GetOp(uniterm)
    return pO


def findFullSip(rtvars, right):
    """docstring for findFullSip"""
    (rt, vars) = rtvars
    if not vars:
        if len(rt) == 1:
            vars = GetArgs(rt[0], secondOrder=True)
        else:
            vars = reduce(lambda l, r:
                          [i for i in GetArgs(l, secondOrder=True) +
                           GetArgs(r, secondOrder=True)
                           if isinstance(i, (Variable, BNode))],
                          rt)
    if len(right) == 1:
        if set(GetArgs(right[0], secondOrder=True)
               ).intersection(vars):  # len(dq)==1:
            #Valid End of recursion, return full SIP order
            yield rt + list(right) if isinstance(right, And) else right
    else:
        #for every combination of left and right, trigger recursive call
        for item in right:
            _vars = set([v for v in GetArgs(item, secondOrder=True)
                         if isinstance(v, (Variable, BNode))])
            _inVars = set([v for v in vars])
            if _vars.intersection(vars):
                # There is an incoming arc, continue processing inductively on
                # the rest of right
                _inVars.update(_vars.difference(vars))
                for sipOrder in findFullSip((rt + [item], _inVars),
                                            [i for i in right if i != item]):
                    yield sipOrder


class InvalidSIPException(Exception):
    def __init__(self, msg=None):
        super(InvalidSIPException, self).__init__(msg)


def BuildNaturalSIP(clause,
                    derivedPreds,
                    adornedHead,
                    hybridPreds2Replace=None,
                    ignoreUnboundDPreds=False):
    """
    Natural SIP:

    Informally, for a rule of a program, a sip represents a decision about the
    order in which the predicates of the rule will be evaluated, and how
    values for variables are passed from predicates to other predicates during
    the evaluation

    >>> from FuXi.Rete.RuleStore import SetupRuleStore # doctest: +SKIP
    >>> ruleStore, ruleGraph = SetupRuleStore(StringIO(PROGRAM2)) # doctest: +SKIP
    >>> ruleStore._finalize() # doctest: +SKIP
    >>> fg = Graph().parse(StringIO(PROGRAM2), format='n3') # doctest: +SKIP
    >>> rs = Ruleset(n3Rules=ruleGraph.store.rules, nsMapping=ruleGraph.store.nsMgr) # doctest: +SKIP
    >>> for rule in rs: print rule # doctest: +SKIP
    Forall ?Y ?X ( ex:sg(?X ?Y) :- ex:flat(?X ?Y) )
    Forall ?Y ?X ?Z4 ?Z1 ?Z2 ?Z3 ( ex:sg(?X ?Y) :- And( ex:up(?X ?Z1) ex:sg(?Z1 ?Z2) ex:flat(?Z2 ?Z3) ex:sg(?Z3 ?Z4) ex:down(?Z4 ?Y) ) )
    >>> sip = BuildNaturalSIP(list(rs)[-1]) # doctest: +SKIP
    >>> for N,x in IncomingSIPArcs(sip, MAGIC.sg): print(N.n3(), x.n3()) # doctest: +SKIP
    ( <http://doi.acm.org/10.1145/28659.28689#up> <http://doi.acm.org/10.1145/28659.28689#sg> <http://doi.acm.org/10.1145/28659.28689#flat> ) ( ?Z3 )
    ( <http://doi.acm.org/10.1145/28659.28689#up> <http://doi.acm.org/10.1145/28659.28689#sg> ) ( ?Z1 )

    >>> sip = BuildNaturalSIP(list(rs)[-1], [MAGIC.sg]) # doctest: +SKIP
    >>> list(sip.query('SELECT ?q {  ?prop a magic:SipArc . [] ?prop ?q . }', initNs={%(u)s'magic':MAGIC})) # doctest: +SKIP
    [rdflib.term.URIRef(%(u)s'http://doi.acm.org/10.1145/28659.28689#sg'), rdflib.term.URIRef(%(u)s'http://doi.acm.org/10.1145/28659.28689#sg')]
    """

    from FuXi.Rete.Magic import AdornedUniTerm
    occurLookup = {}
    boundHead = isinstance(adornedHead, AdornedUniTerm) \
        and 'b' in adornedHead.adornment
    phBoundVars = list(adornedHead.getDistinguishedVariables(varsOnly=True))
    # assert isinstance(clause.head, Uniterm), "Only one literal in the head!"

    def collectSip(left, right):
        if isinstance(left, list):
            vars = CollectSIPArcVars(left, right, phBoundVars)
            if not vars and ignoreUnboundDPreds:
                raise InvalidSIPException("No bound variables for %s" % right)
            leftList = Collection(sipGraph, None)
            left = list(set(left))
            [leftList.append(i) for i in [GetOp(ii) for ii in left]]
            left.append(right)
            # arc = SIPGraphArc(
            #    leftList.uri, getOccurrenceId(
            #       right, occurLookup), vars, sipGraph)
            return left
        else:
            left.isHead = True
            vars = CollectSIPArcVars(left, right, phBoundVars)
            if not vars and ignoreUnboundDPreds:
                raise InvalidSIPException("No bound variables for %s" % right)
            ph = GetOp(left)
            # q = getOccurrenceId(right, occurLookup)
            if boundHead:
                # arc=SIPGraphArc(ph, q, vars, sipGraph, headPassing=boundHead)
                sipGraph.add((ph, RDF.type, MAGIC.BoundHeadPredicate))
                rt = [left, right]
            else:
                rt = [right]
        return rt
    sipGraph = Graph()
    if isinstance(clause.body, And):
        if ignoreUnboundDPreds:
            foundSip = False
            sips = findFullSip(([clause.head], None), clause.body)
            while not foundSip:
                try:
                    sip = next(sips)
                except StopIteration:
                    # Throw SIP exception if sip isn't found (probably means
                    # query + rules combination is 'malformed')
                    raise InvalidSIPException(
                        "Unable to find a sip for %s (%s)" % (
                        clause, adornedHead))
                try:
                    reduce(collectSip,
                           iterCondition(And(sip)))
                    foundSip = True
                    bodyOrder = sip
                except InvalidSIPException:
                    foundSip = False
        else:
            if first(filter(
                    lambda i: isinstance(i, Uniterm)
                     and i.naf or False, clause.body)):
                # There are negative literals in body, ensure
                # the given sip order puts negated literals at the end
                bodyOrder = first(
                    filter(ProperSipOrderWithNegation,
                           findFullSip(([clause.head], None),
                                       clause.body)))
            else:
                bodyOrder = first(findFullSip(
                    ([clause.head], None), clause.body))
            assert bodyOrder, "Couldn't find a valid SIP for %s" % clause
            reduce(collectSip,
                   iterCondition(And(bodyOrder)))
        sipGraph.sipOrder = And(bodyOrder[1:])
        # assert validSip(sipGraph), sipGraph.serialize(format='n3')
    else:
        if boundHead:
            reduce(collectSip, itertools.chain(iterCondition(clause.head),
                                               iterCondition(clause.body)))
        sipGraph.sipOrder = clause.body
    if derivedPreds:
        # We therefore generalize our notation to allow
        # more succinct representation of sips, in which only arcs entering
        # derived predicates are represented.
        arcsToRemove = []
        collectionsToClear = []
        for N, prop, q in sipGraph.query(
            'SELECT ?N ?prop ?q {  ?prop a magic:SipArc . ?N ?prop ?q . }',
                initNs={u'magic': MAGIC}):
            if occurLookup[q] not in derivedPreds \
                and (occurLookup[q] not in
                     hybridPreds2Replace if hybridPreds2Replace else False):
                arcsToRemove.extend([(N, prop, q), (prop, None, None)])
                collectionsToClear.append(Collection(sipGraph, N))
                # clear bindings collection as well
                bindingsColBNode = first(
                    sipGraph.objects(prop, MAGIC.bindings))
                collectionsToClear.append(
                    Collection(sipGraph, bindingsColBNode))
        for removeSts in arcsToRemove:
            sipGraph.remove(removeSts)
        for col in collectionsToClear:
            col.clear()
    return sipGraph


def SIPRepresentation(sipGraph):
    rt = []
    for N, prop, q in sipGraph.query(
        'SELECT ?N ?prop ?q {  ?prop a magic:SipArc . ?N ?prop ?q . }',
            initNs={u'magic': MAGIC}):
        if MAGIC.BoundHeadPredicate in sipGraph.objects(
                subject=N, predicate=RDF.type):
            NCol = [N]
        else:
            NCol = Collection(sipGraph, N)
        rt.append("{ %s } -> %s %s" % (
                  ', '.join([normalizeTerm(term, sipGraph)
                             for term in NCol]),
                  ', '.join([var.n3()
                             for var in Collection(
                             sipGraph,
                             first(sipGraph.objects(prop, MAGIC.bindings)))]),
                  normalizeTerm(q, sipGraph)))
    return rt


def test():
    import doctest
    doctest.testmod()

if __name__ == '__main__':
    test()

# from FuXi.Rete.SidewaysInformationPassing import (
#     BuildNaturalSIP,
#     CollectSIPArcVars,
#     findFullSip,
#     GetArgs,
#     getOccurrenceId,
#     GetOp,
#     GetVariables,
#     IncomingSIPArcs,
#     InvalidSIPException,
#     iterCondition,
#     makeMD5Digest,
#     normalizeTerm,
#     RenderSIPCollection,
#     SetOp,
#     SIPGraphArc,
#     SIPRepresentation,
#     validSip,
#     )

# from FuXi.Rete.SidewaysInformationPassing import BuildNaturalSIP
# from FuXi.Rete.SidewaysInformationPassing import CollectSIPArcVars
# from FuXi.Rete.SidewaysInformationPassing import findFullSip
# from FuXi.Rete.SidewaysInformationPassing import GetArgs
# from FuXi.Rete.SidewaysInformationPassing import getOccurrenceId
# from FuXi.Rete.SidewaysInformationPassing import GetOp
# from FuXi.Rete.SidewaysInformationPassing import GetVariables
# from FuXi.Rete.SidewaysInformationPassing import IncomingSIPArcs
# from FuXi.Rete.SidewaysInformationPassing import InvalidSIPException
# from FuXi.Rete.SidewaysInformationPassing import iterCondition
# from FuXi.Rete.SidewaysInformationPassing import makeMD5Digest
# from FuXi.Rete.SidewaysInformationPassing import normalizeTerm
# from FuXi.Rete.SidewaysInformationPassing import RenderSIPCollection
# from FuXi.Rete.SidewaysInformationPassing import SetOp
# from FuXi.Rete.SidewaysInformationPassing import SIPGraphArc
# from FuXi.Rete.SidewaysInformationPassing import SIPRepresentation
# from FuXi.Rete.SidewaysInformationPassing import validSip
