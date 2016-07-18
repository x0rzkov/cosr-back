import os

from pyspark.sql import types as SparkTypes  # pylint: disable=import-error

from cosrlib.plugins import Plugin
from cosrlib.url import URL



class DomainToDomain(Plugin):
    """ Saves a graph of domain=>domain links in text format """

    hooks = frozenset(["document_post_index", "spark_pipeline_collect", "document_schema"])

    def document_schema(self, schema):
        schema.append(SparkTypes.StructField("external_links", SparkTypes.ArrayType(SparkTypes.StructType([
            SparkTypes.StructField("href", SparkTypes.StringType(), nullable=False)
            # TODO: link text
        ])), nullable=True))

    def document_post_index(self, document, spark_response):
        """ Filters a document post-indexing """

        spark_response["external_links"] = [
            {"href": row["href"].url} for row in document.get_external_hyperlinks()
        ]

    def spark_pipeline_collect(self, sc, sqlc, df, indexer):

        def iter_links_domain(record):
            """ Returns all the parsed links in this record as (from_domain, to_domain) tuples  """

            record_domain = URL(str(record["url"])).normalized_domain
            domains = list(set([
                URL(str(link["href"])).normalized_domain
                for link in record["external_links"]
            ]))

            return [(record_domain, d) for d in domains]

        rdd = df.flatMap(iter_links_domain).distinct().map(lambda row: "%s %s" % row)

        if self.args.get("coalesce"):
            rdd = rdd.coalesce(int(self.args["coalesce"]), shuffle=bool(self.args.get("shuffle")))

        rdd.saveAsTextFile(self.args["dir"])


class DomainToDomainParquet(DomainToDomain):
    """ Saves a graph of domain=>domain links in Apache Parquet format """

    def spark_pipeline_collect(self, sc, sqlc, df, indexer):

        edge_graph_schema = SparkTypes.StructType([
            SparkTypes.StructField("src", SparkTypes.LongType(), nullable=False),
            SparkTypes.StructField("dst", SparkTypes.LongType(), nullable=False)
        ])

        vertex_graph_schema = SparkTypes.StructType([
            SparkTypes.StructField("id", SparkTypes.LongType(), nullable=False),
            SparkTypes.StructField("domain", SparkTypes.StringType(), nullable=False)
        ])

        # @ignore_exceptions([])
        def iter_links_domain(record):
            """ Returns all the parsed links in this record as (from_domain, to_domain) tuples  """

            record_domain = indexer.urlclient.get_domain_id(record["url"])
            domains = list(set([URL(str(link["href"])).homepage for link in record["external_links"]]))
            link_ids = set(indexer.urlclient.get_domain_ids(domains))
            link_ids.discard(record_domain)

            return [{"src": record_domain, "dst": d} for d in link_ids]

        rdd = df.flatMap(iter_links_domain)

        edge_df = sqlc.createDataFrame(rdd, edge_graph_schema)

        edge_df = edge_df.distinct()

        if self.args.get("coalesce"):
            edge_df = edge_df.coalesce(int(self.args["coalesce"]))

        edge_df.write.parquet(os.path.join(self.args["dir"], "edges"))

        def iter_domain(record):
            """ Returns all the domains found in links """

            domains = set([URL(str(link["href"])).normalized_domain for link in record["external_links"]])
            domains.add(URL(str(record["url"])).normalized_domain)

            return list(domains)

        def add_domain_id(domain):
            return {
                "id": indexer.urlclient.get_domain_id("http://%s" % domain),
                "domain": domain
            }

        rdd_domains = df.flatMap(iter_domain).distinct().map(add_domain_id)

        vertex_df = sqlc.createDataFrame(rdd_domains, vertex_graph_schema).coalesce(1)

        vertex_df.write.parquet(os.path.join(self.args["dir"], "vertices"))