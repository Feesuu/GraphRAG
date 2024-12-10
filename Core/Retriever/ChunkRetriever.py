from Core.Common.Logger import logger
from Core.Retriever.BaseRetriever import BaseRetriever
import asyncio
import json
import numpy as np
from Core.Common.Utils import truncate_list_by_token_size, split_string_by_multi_markers, min_max_normalize
from collections import defaultdict, Counter
from Core.Retriever.RetrieverFactory import register_retriever_method
from Core.Common.Constants import GRAPH_FIELD_SEP

class ChunkRetriever(BaseRetriever):
    def __init__(self, **kwargs):

        config = kwargs.pop("config")
        super().__init__(config)
        self.mode_list = ["entity_occurrence", "ppr", "from_relation"]
        self.type = "chunk"
        for key, value in kwargs.items():
            setattr(self, key, value)
            
    @register_retriever_method(type = "chunk", method_name="entity_occurrence")       
    async def _find_relevant_chunks_from_entitiy_occurrence(self, node_datas: list[dict]):
        
        if len(node_datas) == 0:
            return None
        text_units = [
            split_string_by_multi_markers(dp["source_id"], [GRAPH_FIELD_SEP])
            for dp in node_datas
        ]
        edges = await asyncio.gather(
            *[self.graph.get_node_edges(dp["entity_name"]) for dp in node_datas]
        )
        all_one_hop_nodes = set()
        for this_edges in edges:
            if not this_edges:
                continue
            all_one_hop_nodes.update([e[1] for e in this_edges])
        all_one_hop_nodes = list(all_one_hop_nodes)
        all_one_hop_nodes_data = await asyncio.gather(
            *[self.graph.get_node(e) for e in all_one_hop_nodes]
        )
        all_one_hop_text_units_lookup = {
            k: set(split_string_by_multi_markers(v["source_id"], [GRAPH_FIELD_SEP]))
            for k, v in zip(all_one_hop_nodes, all_one_hop_nodes_data)
            if v is not None
        }
        all_text_units_lookup = {}
        for index, (this_text_units, this_edges) in enumerate(zip(text_units, edges)):
            for c_id in this_text_units:
                if c_id in all_text_units_lookup:
                    continue
                relation_counts = 0
                for e in this_edges:
                    if (
                            e[1] in all_one_hop_text_units_lookup
                            and c_id in all_one_hop_text_units_lookup[e[1]]
                    ):
                        relation_counts += 1
                all_text_units_lookup[c_id] = {
                    "data": await self.doc_chunk.get_data_by_key(c_id),
                    "order": index,
                    "relation_counts": relation_counts,
                }
        if any([v is None for v in all_text_units_lookup.values()]):
            logger.warning("Text chunks are missing, maybe the storage is damaged")
        all_text_units = [
            {"id": k, **v} for k, v in all_text_units_lookup.items() if v is not None
        ]   
        # for node_data in node_datas:
        all_text_units = sorted(
            all_text_units, key=lambda x: (x["order"], -x["relation_counts"])
        )
        all_text_units = truncate_list_by_token_size(
            all_text_units,
            key=lambda x: x["data"],
            max_token_size=self.config.local_max_token_for_text_unit,
        )
        all_text_units = [t["data"] for t in all_text_units]

        return all_text_units

    @register_retriever_method(type = "chunk", method_name="from_relation")
    async def _find_relevant_chunks_from_relationships(self, seed: list[dict]):
            text_units = [
                split_string_by_multi_markers(dp["source_id"], [GRAPH_FIELD_SEP])
                for dp in seed
            ]

            all_text_units_lookup = {}

            for index, unit_list in enumerate(text_units):
                for c_id in unit_list:
                    if c_id not in all_text_units_lookup:
                        all_text_units_lookup[c_id] = {
                            "data": await self.doc_chunk.get_data_by_key(c_id),
                            "order": index,
                        }

            if any([v is None for v in all_text_units_lookup.values()]):
                logger.warning("Text chunks are missing, maybe the storage is damaged")
            all_text_units = [
                {"id": k, **v} for k, v in all_text_units_lookup.items() if v is not None
            ]
            all_text_units = sorted(all_text_units, key=lambda x: x["order"])
            all_text_units = truncate_list_by_token_size(
                all_text_units,
                key=lambda x: x["data"],
                max_token_size = self.config.max_token_for_text_unit,
            )
            all_text_units = [t["data"] for t in all_text_units]

            return all_text_units
        
    @register_retriever_method(type = "chunk", method_name="ppr")
    async def _find_relevant_chunks_by_ppr(self, query, seed_entities: list[dict]):
        # 
        entity_to_edge_mat = await self.entities_to_relationships.get()
        relationship_to_chunk_mat = await self.relationships_to_chunks.get()
        import pdb
        pdb.set_trace()
        # Create a vector (num_doc) with 1s at the indices of the retrieved documents and 0s elsewhere
        node_ppr_matrix = await self._run_personalized_pagerank(query, seed_entities)
        edge_prob = entity_to_edge_mat.T.dot(node_ppr_matrix)
        ppr_chunk_prob = relationship_to_chunk_mat.T.dot(edge_prob)
        ppr_chunk_prob = min_max_normalize(ppr_chunk_prob)
        # Return top k documents
        sorted_doc_ids = np.argsort(ppr_chunk_prob, kind='mergesort')[::-1]
        sorted_scores = ppr_chunk_prob[sorted_doc_ids]
        top_k = self.config.top_k
        soreted_docs = await self.doc_chunk.get_data_by_indices(sorted_doc_ids[:top_k])
        return soreted_docs, sorted_scores[:top_k]