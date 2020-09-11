////////////////////////////////////////////////////////////////////////////////
//
//  @file Server_Element.h
//
//  @brief Server_Element header, represents a unique element
//
//  @copy 2020 Elementary Robotics. All rights reserved.
//
////////////////////////////////////////////////////////////////////////////////



#ifndef __ATOM_CPP_SERVER_ELEMENT_H
#define __ATOM_CPP_SERVER_ELEMENT_H

#include <iostream>

#include "ConnectionPool.h"
#include "Serialization.h"
#include "Redis.h"
#include "Logger.h"
#include "Messages.h"


namespace atom{

template<typename ConnectionType, typename BufferType>
class Server_Element {
    public:
        Server_Element(boost::asio::io_context & iocon, int max_cons, int timeout, std::string redis_ip, 
                    Serialization& serialization, int num_buffs, int buff_timeout,
                    int num_tcp, int num_unix, 
                    std::ostream& log_stream, std::string element_name);

        virtual ~Server_Element();

        ///Writes an entry to Redis server
        ///@tparam ConnectionType determines if TCP or UNIX socket is used
        ///@tparam BufferType determines the underlying buffer type
        ///@param stream_name name of the stream to write to
        ///@param entry_data vector of redis keys and values to write to redis. Key value pairs must be specified in sequence.
        ///@param ser_method the serialization method to use
        ///@param err to hold errors that may occur during this operation
        ///@tparam DataType can be std::vector<msgpack::type::variant> or std::vector<std::string> for no serialization
        template<typename DataType>
        atom::redis_reply<BufferType> entry_write(std::string stream_name,  
                                DataType& entry_data, atom::Serialization::method ser_method, atom::error& err){
            //make sure vector isn't empty
            if(!(entry_data.size() > 0)){
                logger.alert("Writing empty vector to Redis is not permitted.");
                err.set_error_code(atom::error_codes::invalid_command);
                return atom::redis_reply<BufferType>(0, nullptr);
            }

            //make sure each redis key has a corresponding value
            if((entry_data.size() % 2 ) > 0){
                logger.alert("Invalid entry data. Each redis key must have a corresponding value.");
                err.set_error_code(atom::error_codes::invalid_command);
                return atom::redis_reply<BufferType>(0, nullptr);
            }

            //check for invalid keys
            std::vector<std::string> reserved_keys = atom::reserved_keys.at("entry_keys");
            bool is_key = true;
            for(const auto & m: entry_data) {
                if(is_key){
                    try{ boost::get<std::string>(m);} catch(boost::bad_get &e ){
                        logger.alert("Redis keys must be strings.");
                        throw std::runtime_error("Redis keys must be strings.");
                    }
                    if(std::find(reserved_keys.begin(), reserved_keys.end(), boost::get<std::string>(m)) != reserved_keys.end()){
                        logger.alert("Invalid key: " + boost::get<std::string>(m) + " is a reserved key.");
                        err.set_error_code(atom::error_codes::invalid_command);
                        return atom::redis_reply<BufferType>(0, nullptr);
                    }
                }
                is_key = !is_key;
            }

            //add stream
            streams.push_back(stream_name);
            auto processed_data = ser.serialize(entry_data, ser_method, err);
            return connection->xadd(stream_name, ser.method_strings.at(ser_method), processed_data, err);
        };

    private:
        std::string name;
        std::map<std::string, atom::reference> references;
        std::map<std::string, atom::CommandHandler<BufferType>> command_handlers;
        std::vector<std::string> streams;
        std::string atom_version;
        std::string atom_language;
        ConnectionPool pool;
        std::shared_ptr<ConnectionType> connection;
        Serialization ser;
        Logger logger;
};

};

#endif //__ATOM_CPP_SERVER_ELEMENT_H