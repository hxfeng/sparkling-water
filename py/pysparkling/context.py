from pyspark.context import SparkContext
from pyspark.sql.dataframe import DataFrame
from pyspark.rdd import RDD
from pyspark.sql import SparkSession
from h2o.frame import H2OFrame
from pysparkling.initializer import Initializer
from pysparkling.conf import H2OConf
import h2o
from pysparkling.conversions import FrameConversions as fc
import warnings
import atexit

def _monkey_patch_H2OFrame(hc):
    @staticmethod
    def determine_java_vec_type(vec):
        if vec.isCategorical():
            return "enum"
        elif vec.isUUID():
            return "uuid"
        elif vec.isString():
            return "string"
        elif vec.isInt():
            if vec.isTime():
                return "time"
            else:
                return "int"
        else:
            return "real"

    def get_java_h2o_frame(self):
        # Can we use cached H2O frame?
        # Only if we cached it before and cache was not invalidated by rapids expression
        if not hasattr(self, '_java_frame') or self._java_frame is None \
           or self._ex._cache._id is None or self._ex._cache.is_empty() \
           or not self._ex._cache._id == self._java_frame_sid:
            # Note: self.frame_id will trigger frame evaluation
            self._java_frame = hc._jhc.asH2OFrame(self.frame_id)
        return self._java_frame

    @staticmethod
    def from_java_h2o_frame(h2o_frame, h2o_frame_id):
        # Cache Java reference to the backend frame
        sid = h2o_frame_id.toString()
        fr = H2OFrame.get_frame(sid)
        fr._java_frame = h2o_frame
        fr._java_frame_sid = sid
        fr._backed_by_java_obj = True
        return fr
    H2OFrame.determine_java_vec_type = determine_java_vec_type
    H2OFrame.from_java_h2o_frame = from_java_h2o_frame
    H2OFrame.get_java_h2o_frame = get_java_h2o_frame

def _is_of_simple_type(rdd):
    if not isinstance(rdd, RDD):
        raise ValueError('rdd is not of type pyspark.rdd.RDD')

    if isinstance(rdd.first(), (str, int, bool, long, float)):
        return True
    else:
        return False


def _get_first(rdd):
    if rdd.isEmpty():
        raise ValueError('rdd is empty')

    return rdd.first()


class H2OContext(object):

    def __init__(self, spark_session):
        """
         This constructor is used just to initialize the environment. It does not start H2OContext.
         To start H2OContext use one of the getOrCreate methods. This constructor is internally used in those methods
        """
        try:
            self.__do_init(spark_session)
            _monkey_patch_H2OFrame(self)
            # loads sparkling water jar only if it hasn't been already loaded
            Initializer.load_sparkling_jar(self._sc)
        except:
            raise

    def __do_init(self, spark_session):
        self._ss = spark_session
        self._sc = self._ss._sc
        self._sql_context = self._ss._wrapped
        self._jsql_context = self._ss._jwrapped
        self._jsc = self._sc._jsc
        self._jvm = self._sc._jvm
        self._gw = self._sc._gateway

        self.is_initialized = False

    @staticmethod
    def getOrCreate(spark, conf=None, **kwargs):
        """
         Get existing or create new H2OContext based on provided H2O configuration. If the conf parameter is set then
         configuration from it is used. Otherwise the configuration properties passed to Sparkling Water are used.
         If the values are not found the default values are used in most of the cases. The default cluster mode
         is internal, ie. spark.ext.h2o.external.cluster.mode=false

         param - Spark Context or Spark Session
         returns H2O Context
        """

        spark_session = spark
        if isinstance(spark, SparkContext):
            warnings.warn("Method H2OContext.getOrCreate with argument of type SparkContext is deprecated and " +
                          "parameter of type SparkSession is preferred.")
            spark_session = SparkSession.builder.getOrCreate()

        h2o_context = H2OContext(spark_session)

        jvm = h2o_context._jvm  # JVM
        jsc = h2o_context._jsc  # JavaSparkContext

        if conf is not None:
            selected_conf = conf
        else:
            selected_conf = H2OConf(spark_session)
        # Create backing Java H2OContext
        jhc = jvm.org.apache.spark.h2o.JavaH2OContext.getOrCreate(jsc, selected_conf._jconf)
        h2o_context._jhc = jhc
        h2o_context._conf = selected_conf
        h2o_context._client_ip = jhc.h2oLocalClientIp()
        h2o_context._client_port = jhc.h2oLocalClientPort()
        # Create H2O REST API client
        h2o.connect(ip=h2o_context._client_ip, port=h2o_context._client_port, **kwargs)
        h2o_context.is_initialized = True
        # Stop h2o when running standalone pysparkling scripts, only in client deploy mode
        #, so the user does not explicitly close h2o.
        # In driver mode the application would call exit which is handled by Spark AM as failure
        deploy_mode = spark_session.sparkContext._conf.get("spark.submit.deployMode")
        if deploy_mode != "cluster":
            atexit.register(lambda: h2o_context.stop_with_jvm())
        return h2o_context


    def stop_with_jvm(self):
        h2o.cluster().shutdown()
        self.stop()


    def stop(self):
        warnings.warn("Stopping H2OContext. (Restarting H2O is not yet fully supported...) ")
        self._jhc.stop(False)

    def __del__(self):
        self.stop()

    def __str__(self):
        if self.is_initialized:
            return "H2OContext: ip={}, port={} (open UI at http://{}:{} )".format(self._client_ip, self._client_port, self._client_ip, self._client_port)
        else:
            return "H2OContext: not initialized, call H2OContext.getOrCreate(sc) or H2OContext.getOrCreate(sc, conf)"

    def __repr__(self):
        self.show()
        return ""

    def show(self):
        print(self)

    def get_conf(self):
        return self._conf

    def as_spark_frame(self, h2o_frame, copy_metadata=True):
        """
        Transforms given H2OFrame to Spark DataFrame

        Parameters
        ----------
          h2o_frame : H2OFrame
          copy_metadata: Bool = True

        Returns
        -------
          Spark DataFrame
        """
        if isinstance(h2o_frame, H2OFrame):
            j_h2o_frame = h2o_frame.get_java_h2o_frame()
            jdf = self._jhc.asDataFrame(j_h2o_frame, copy_metadata, self._jsql_context)
            df = DataFrame(jdf, self._sql_context)
            # Attach h2o_frame to dataframe which forces python not to delete the frame when we leave the scope of this
            # method.
            # Without this, after leaving this method python would garbage collect the frame since it's not used
            # anywhere and spark. when executing any action on this dataframe, will fail since the frame
            # would be missing.
            df._h2o_frame = h2o_frame
            return df

    def as_h2o_frame(self, dataframe, framename=None):
        """
        Transforms given Spark RDD or DataFrame to H2OFrame.

        Parameters
        ----------
          dataframe : Spark RDD or DataFrame
          framename : Optional name for resulting H2OFrame

        Returns
        -------
          H2OFrame which contains data of original input Spark data structure
        """
        if isinstance(dataframe, DataFrame):
            return fc._as_h2o_frame_from_dataframe(self, dataframe, framename)
        elif isinstance(dataframe, RDD):
            # First check if the type T in RDD[T] is one of the python "primitive" types
            # String, Boolean, Int and Double (Python Long is converted to java.lang.BigInteger)
            if _is_of_simple_type(dataframe):
                first = _get_first(dataframe)
                if isinstance(first, str):
                    return fc._as_h2o_frame_from_RDD_String(self, dataframe, framename)
                elif isinstance(first, bool):
                    return fc._as_h2o_frame_from_RDD_Bool(self, dataframe, framename)
                elif isinstance(dataframe.min(), int) and isinstance(dataframe.max(), int):
                    if dataframe.min() >= self._jvm.Integer.MIN_VALUE and dataframe.max() <= self._jvm.Integer.MAX_VALUE:
                        return fc._as_h2o_frame_from_RDD_Int(self, dataframe, framename)
                    else:
                        return fc._as_h2o_frame_from_RDD_Long(self, dataframe, framename)
                elif isinstance(first, float):
                    return fc._as_h2o_frame_from_RDD_Float(self, dataframe, framename)
                elif isinstance(dataframe.max(), long):
                    raise ValueError('Numbers in RDD Too Big')
            else:
                return fc._as_h2o_frame_from_complex_type(self, dataframe, framename)

