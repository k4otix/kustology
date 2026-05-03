from .bridge import SyntaxVisitor

class KustoVisitor(SyntaxVisitor):
    """
    Pythonic base class for visiting KQL AST nodes.
    """
    def Visit(self, node):
        if node is None:
            return
        super().Visit(node)

    def VisitDefault(self, node):
        # Default behavior: visit children
        if node is not None:
            for i in range(node.ChildCount):
                child = node.GetChild(i)
                if child is not None:
                    self.Visit(child)
