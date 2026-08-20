import cyal.exceptions

class Sound:
    def __init__(self, source, volume, dist=False, cat="miscelaneous"):
        self._source = source
        self.volume=volume
        self.cat=cat
        self.dist = dist
        self.muted = False
        self.force_to_destroy = False

    def destroy(self, force=False):
        if self.force_to_destroy and not force: return
        if self.source is None: return
        # cyal's Source has no explicit delete() method: the AL source is
        # released by Source.__dealloc__ once the last Python reference
        # (this wrapper) is dropped. The old code called the nonexistent
        # .delete() and swallowed the AttributeError, so sources were never
        # explicitly released and lingered until GC ran on some thread.
        try: self._source.stop()
        except cyal.exceptions.InvalidOperationError: pass
        self._source = None
    @property
    def source(self):
        return self._source
    
    @source.setter
    def source(self, value):
        self._source = value
    
    @source.deleter
    def source(self):
        del self._source
        self._source = None
        
