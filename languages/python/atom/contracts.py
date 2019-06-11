from lazycontract import BinaryProperty, LazyContract

class BinaryProperty(LazyProperty):
    _type = bytes
    def deserialize(self, obj):
        return obj if isinstance(obj, self._type) else None

class RawContract(LazyContract):
    data = BinaryProperty(required=True)

    def to_dict():
        raise TypeError("Cannot convert raw contract to dict, use the to_data() function instead")

    def to_data():
        return self.data
